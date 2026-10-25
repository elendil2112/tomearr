"""Phase 8: search your indexers through Prowlarr.

Prowlarr already knows every indexer you use (torrent and usenet) and searches them in parallel. Tomearr only asks
it for book releases and scores what comes back against the book you want (see releases.py).
"""
import threading
import time

import httpx

from . import db

BOOK_CATEGORIES = (7000, 7020)          # Books, Books/EBook (newznab numbering)


class ProwlarrError(Exception):
    pass


def config(conn) -> dict:
    return {"url": (db.get_meta(conn, "setting_prowlarr_url", "") or "").rstrip("/"),
            "api_key": db.get_meta(conn, "setting_prowlarr_api_key", "") or ""}


class Prowlarr:
    def __init__(self, url: str, api_key: str, client: httpx.Client | None = None, timeout: float = 90):
        if not url or not api_key:
            raise ProwlarrError("Prowlarr isn't set up yet: add its address and API key in Settings")
        self.url, self.key = url.rstrip("/"), api_key
        self.client = client or httpx.Client(timeout=timeout)

    def _get(self, path: str, params=None):
        try:
            r = self.client.get(f"{self.url}{path}", params=params, headers={"X-Api-Key": self.key})
        except httpx.HTTPError as exc:
            raise ProwlarrError(f"can't reach Prowlarr at {self.url}: {exc}") from exc
        if r.status_code == 401:
            raise ProwlarrError("Prowlarr refused the API key (Prowlarr → Settings → General → API Key)")
        if r.status_code >= 400:
            raise ProwlarrError(f"Prowlarr answered {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise ProwlarrError(f"{self.url} doesn't look like Prowlarr (not JSON)") from exc

    def test(self) -> dict:
        st = self._get("/api/v1/system/status")
        idx = [i for i in self._get("/api/v1/indexer") if i.get("enable")]
        return {"ok": True, "version": st.get("version"),
                "indexers": [{"id": i["id"], "name": i["name"], "protocol": i.get("protocol")} for i in idx]}

    def search(self, query: str, limit: int = 100) -> list[dict]:
        params = [("query", query), ("type", "search"), ("limit", str(limit)), ("offset", "0")]
        params += [("categories", str(c)) for c in BOOK_CATEGORIES]
        rows = self._get("/api/v1/search", params)
        return rows if isinstance(rows, list) else []

    def fetch(self, url: str) -> tuple[str, bytes | str]:
        """Fetch a release's download link (through Prowlarr). Returns ('magnet', uri) or ('file', bytes).

        Redirects are followed by hand: indexers often answer a torrent download with a redirect to a magnet link,
        which an HTTP client can't follow.
        """
        for _ in range(6):
            if url.startswith("magnet:"):
                return "magnet", url
            try:
                # the API key only goes to Prowlarr, never to an indexer a link redirects to
                headers = {"X-Api-Key": self.key} if url.startswith(self.url + "/") else {}
                r = self.client.get(url, follow_redirects=False, headers=headers)
            except httpx.HTTPError as exc:
                raise ProwlarrError(f"couldn't download the release: {exc}") from exc
            if r.is_redirect:
                url = r.headers.get("location", "")
                continue
            if r.status_code >= 400:
                raise ProwlarrError(f"the indexer answered {r.status_code} for the download")
            if not r.content:
                raise ProwlarrError("the indexer returned an empty file")
            return "file", r.content
        raise ProwlarrError("too many redirects fetching the release")


# Search results are kept for a while so a grab can refer to a release by id, instead of the browser sending back
# a download URL (which would let any page make Tomearr fetch any URL).
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
CACHE_SECONDS = 6 * 3600


def remember(releases: list[dict]) -> None:
    now = time.time()
    with _LOCK:
        for k in [k for k, (t, _) in _CACHE.items() if now - t > CACHE_SECONDS]:
            del _CACHE[k]
        for r in releases:
            _CACHE[r["id"]] = (now, r)


def recall(release_id: str) -> dict | None:
    with _LOCK:
        hit = _CACHE.get(release_id)
    return hit[1] if hit else None
