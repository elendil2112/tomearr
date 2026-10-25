"""Phase 9: send releases to a download client and follow them until they finish.

Torrents go to Deluge (through its web UI's JSON-RPC API, the same one Sonarr and Prowlarr use), NZBs to SABnzbd.
Tomearr fetches the .torrent / .nzb itself (through Prowlarr) and uploads it, so the client never needs to reach
Prowlarr or the indexer. Finished downloads should land in the ingest folder: set the Deluge label's or the
SABnzbd category's completed folder to it (or give Deluge a download folder in Settings).
"""
import base64
import re
from urllib.parse import parse_qs, urlparse

import httpx

from . import db


class ClientError(Exception):
    pass


def setting(conn, key: str, default: str = "") -> str:
    return db.get_meta(conn, f"setting_{key}", default) or default


# --- Deluge ----------------------------------------------------------------------------------------------

class Deluge:
    FIELDS = ["name", "state", "progress", "is_finished", "save_path", "total_size", "message", "eta"]

    def __init__(self, url: str, password: str, client: httpx.Client | None = None):
        if not url:
            raise ClientError("Deluge isn't set up yet: add its web UI address in Settings")
        self.url = url.rstrip("/")
        if not self.url.endswith("/json"):
            self.url += "/json"
        self.password = password
        self.client = client or httpx.Client(timeout=30)
        self._id = 0
        self._ready = False

    def _call(self, method: str, *params):
        self._id += 1
        try:
            r = self.client.post(self.url, json={"method": method, "params": list(params), "id": self._id})
        except httpx.HTTPError as exc:
            raise ClientError(f"can't reach Deluge at {self.url}: {exc}") from exc
        if r.status_code >= 400:
            raise ClientError(f"Deluge answered {r.status_code} (is this the web UI address, port 8112?)")
        try:
            body = r.json()
        except ValueError as exc:
            raise ClientError(f"{self.url} doesn't look like Deluge's web UI") from exc
        if body.get("error"):
            msg = body["error"].get("message") if isinstance(body["error"], dict) else body["error"]
            raise ClientError(f"Deluge: {msg}")
        return body.get("result")

    def connect(self) -> None:
        if self._ready:
            return
        if not self._call("auth.login", self.password):
            raise ClientError("Deluge refused the password (the web UI password)")
        if not self._call("web.connected"):
            hosts = self._call("web.get_hosts") or []
            if not hosts:
                raise ClientError("Deluge's web UI has no daemon to connect to (Connection Manager)")
            self._call("web.connect", hosts[0][0])
            if not self._call("web.connected"):
                raise ClientError("Deluge's web UI couldn't connect to its daemon")
        self._ready = True

    def test(self) -> dict:
        self.connect()
        plugins = self._call("core.get_enabled_plugins") or []
        version = None
        for m in ("daemon.info", "daemon.get_version"):
            try:
                version = self._call(m)
                break
            except ClientError:
                pass
        return {"ok": True, "version": version, "label_plugin": "Label" in plugins}

    def add(self, kind: str, payload, name: str, label: str = "", path: str = "") -> tuple[str, str | None]:
        """Add a magnet or .torrent. Returns (torrent hash, note)."""
        self.connect()
        opts = {}
        if path:
            opts["download_location"] = path
        if kind == "magnet":
            try:
                tid = self._call("core.add_torrent_magnet", payload, opts)
            except ClientError as exc:
                tid = _already(exc, magnet_hash(payload))
        else:
            try:
                tid = self._call("core.add_torrent_file", f"{_safe(name)}.torrent",
                                 base64.b64encode(payload).decode(), opts)
            except ClientError as exc:
                tid = _already(exc, None)
        if not tid:
            tid = magnet_hash(payload) if kind == "magnet" else None
        if not tid:
            raise ClientError("Deluge didn't accept the torrent (it may already be in Deluge)")
        note = None
        if label:
            try:
                labels = self._call("label.get_labels") or []
                if label.lower() not in [x.lower() for x in labels]:
                    self._call("label.add", label.lower())
                self._call("label.set_torrent", tid, label.lower())
            except ClientError as exc:
                note = f"added, but couldn't set the label ({exc}); is the Label plugin enabled?"
        return tid.lower(), note

    def status(self, hashes: list[str]) -> dict[str, dict]:
        if not hashes:
            return {}
        self.connect()
        res = self._call("core.get_torrents_status", {"id": hashes}, self.FIELDS) or {}
        out = {}
        for h, t in res.items():
            st = (t.get("state") or "").lower()
            done = bool(t.get("is_finished")) or (t.get("progress") or 0) >= 100
            out[h.lower()] = {
                "state": "failed" if st == "error" else "completed" if done else
                         "queued" if st in ("queued", "checking", "allocating") else "downloading",
                "progress": round(t.get("progress") or 0, 1), "name": t.get("name"),
                "message": t.get("message") if st == "error" else None, "path": t.get("save_path"),
            }
        return out


def _already(exc: ClientError, h: str | None) -> str | None:
    if "already" in str(exc).lower():
        return h or _hash_in(str(exc))
    raise exc


def _hash_in(s: str) -> str | None:
    m = re.search(r"\b([0-9a-f]{40})\b", s.lower())
    return m.group(1) if m else None


def magnet_hash(uri: str) -> str | None:
    try:
        xt = parse_qs(urlparse(uri).query).get("xt", [])
    except ValueError:
        return None
    for x in xt:
        if x.lower().startswith("urn:btih:"):
            h = x[9:]
            if len(h) == 32:   # base32
                try:
                    return base64.b32decode(h.upper()).hex()
                except ValueError:
                    return None
            return h.lower()
    return None


def _safe(name: str) -> str:
    return re.sub(r"[^\w .()\[\]-]", "_", name)[:150] or "release"


# --- SABnzbd ---------------------------------------------------------------------------------------------

class Sabnzbd:
    def __init__(self, url: str, api_key: str, client: httpx.Client | None = None):
        if not url or not api_key:
            raise ClientError("SABnzbd isn't set up yet: add its address and API key in Settings")
        self.url = url.rstrip("/")
        if not self.url.endswith("/api"):
            self.url += "/api"
        self.key = api_key
        self.client = client or httpx.Client(timeout=30)

    def _req(self, params: dict, files=None):
        p = {**params, "apikey": self.key, "output": "json"}
        try:
            r = (self.client.post(self.url, params=p, files=files) if files else self.client.get(self.url, params=p))
        except httpx.HTTPError as exc:
            raise ClientError(f"can't reach SABnzbd at {self.url}: {exc}") from exc
        if r.status_code >= 400:
            raise ClientError(f"SABnzbd answered {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise ClientError(f"{self.url} doesn't look like SABnzbd ({r.text[:80]})") from exc
        if isinstance(body, dict) and body.get("status") is False:
            raise ClientError(f"SABnzbd: {body.get('error') or 'refused'}")
        if isinstance(body, dict) and "error" in body and len(body) == 1:
            raise ClientError(f"SABnzbd: {body['error']}")
        return body

    def test(self) -> dict:
        v = self._req({"mode": "version"}).get("version")
        cats = self._req({"mode": "get_cats"}).get("categories", [])
        return {"ok": True, "version": v, "categories": cats}

    def add(self, nzb: bytes, name: str, category: str = "") -> str:
        params = {"mode": "addfile", "nzbname": name}
        if category:
            params["cat"] = category
        body = self._req(params, files={"name": (f"{_safe(name)}.nzb", nzb, "application/x-nzb")})
        ids = body.get("nzo_ids") or []
        if not ids:
            raise ClientError("SABnzbd didn't accept the NZB")
        return ids[0]

    def status(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        out: dict[str, dict] = {}
        q = self._req({"mode": "queue", "nzo_ids": ",".join(ids)}).get("queue", {})
        for s in q.get("slots", []):
            st = (s.get("status") or "").lower()
            out[s["nzo_id"]] = {"state": "queued" if st in ("queued", "paused", "grabbing", "propagating")
                                else "downloading", "progress": float(s.get("percentage") or 0),
                                "name": s.get("filename"), "message": None, "path": None}
        rest = [i for i in ids if i not in out]
        if rest:
            h = self._req({"mode": "history", "nzo_ids": ",".join(rest)}).get("history", {})
            for s in h.get("slots", []):
                st = (s.get("status") or "").lower()
                out[s["nzo_id"]] = {"state": "completed" if st == "completed" else "failed" if st == "failed"
                                    else "downloading", "progress": 100.0 if st == "completed" else 99.0,
                                    "name": s.get("name"), "message": s.get("fail_message") or None,
                                    "path": s.get("storage")}
        return out


def deluge(conn, client=None) -> Deluge:
    return Deluge(setting(conn, "deluge_url"), setting(conn, "deluge_password"), client)


def sabnzbd(conn, client=None) -> Sabnzbd:
    return Sabnzbd(setting(conn, "sab_url"), setting(conn, "sab_api_key"), client)
