"""Phases 8-9: Prowlarr search, release scoring, grabbing to Deluge / SABnzbd, following downloads."""
import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clients, db, grabs, ingest, library, monitor, prowlarr, releases
from conftest import make_calibre
from test_catalog import _no, _noop, conn  # noqa: F401  (shared fixture)
from test_phase7 import EXTRA, TOLKIEN, _add_works

HURIN = "OL100W"
TORRENT = b"d8:announce3:foo4:infod4:name4:testee"
NZB = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'
MAGNET = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=hurin"


def _rel(title, protocol="torrent", seeders=10, size=2_000_000, cats=(7020,), guid=None, link=True, magnet=None):
    g = guid or title
    return {"guid": g, "indexerId": 3 if protocol == "torrent" else 4,
            "indexer": "BookTracker" if protocol == "torrent" else "NZBGeek", "title": title, "size": size,
            "protocol": protocol, "seeders": seeders if protocol == "torrent" else None, "leechers": 1,
            "categories": [{"id": c, "name": "x"} for c in cats], "age": 12, "publishDate": "2024-01-01T00:00:00Z",
            "downloadUrl": f"http://prowlarr:9696/3/download?link={g.replace(' ', '_')}" if link else None,
            "magnetUrl": magnet, "infoUrl": "http://tracker/details/1"}


RESULTS = [
    _rel("J.R.R. Tolkien - The Children of Hurin (2007) [Retail EPUB]", seeders=25),
    _rel("Tolkien - The Children of Hurin MP3 Unabridged", cats=(3030,)),
    _rel("Die Kinder Hurins - Tolkien [GER] epub"),
    _rel("J R R Tolkien The Children of Hurin mobi", protocol="usenet"),
    _rel("The Children of Hurin - Tolkien.pdf", seeders=0),
    _rel("Tolkien - The Children of Hurin (magnet)", link=False, magnet=MAGNET, seeders=3),
    # some indexers: no download link, and the "magnet" link is a Prowlarr address that redirects to the magnet
    _rel("Tolkien - The Children of Hurin (proxied magnet)", link=False, seeders=4,
         magnet="http://prowlarr:9696/3/download?link=proxied&file=Hurin.epub"),
]


class Fake:
    """Prowlarr, Deluge and SABnzbd in one httpx transport."""

    def __init__(self):
        self.searches, self.deluge_calls, self.sab_adds = [], [], []
        self.torrents: dict[str, dict] = {}
        self.sab_queue: dict[str, dict] = {}
        self.sab_hist: dict[str, dict] = {}
        self.redirect_to_magnet = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        if u.host == "prowlarr":
            if req.headers.get("x-api-key") != "PKEY":
                return httpx.Response(401)
            if u.path == "/api/v1/system/status":
                return httpx.Response(200, json={"version": "1.20"})
            if u.path == "/api/v1/indexer":
                return httpx.Response(200, json=[{"id": 3, "name": "BookTracker", "protocol": "torrent", "enable": True},
                                                 {"id": 4, "name": "NZBGeek", "protocol": "usenet", "enable": True},
                                                 {"id": 5, "name": "Off", "protocol": "torrent", "enable": False}])
            if u.path == "/api/v1/search":
                q = parse_qs(u.query.decode())
                self.searches.append((q["query"][0], q["categories"]))
                return httpx.Response(200, json=RESULTS)
            if u.path.endswith("/download"):
                if self.redirect_to_magnet or "link=proxied" in str(u):
                    return httpx.Response(302, headers={"location": MAGNET})
                return httpx.Response(200, content=NZB if "/4/" in u.path or "mobi" in str(u) else TORRENT)
        if u.host == "deluge":
            body = json.loads(req.content)
            m, p = body["method"], body["params"]
            self.deluge_calls.append(m)
            res = None
            if m == "auth.login":
                res = p[0] == "secret"
            elif m == "web.connected":
                res = "web.connect" in self.deluge_calls
            elif m == "web.get_hosts":
                res = [["host1", "127.0.0.1", 58846, "Online"]]
            elif m == "core.get_enabled_plugins":
                res = ["Label"]
            elif m == "label.get_labels":
                res = []
            elif m in ("core.add_torrent_file", "core.add_torrent_magnet"):
                h = "abcdef0123456789abcdef0123456789abcdef01" if m.endswith("magnet") else "11" * 20
                if h in self.torrents:
                    return httpx.Response(200, json={"id": body["id"], "result": None,
                                                     "error": {"message": f"Torrent already in session ({h})."}})
                self.torrents[h] = {"name": "Hurin", "state": "Downloading", "progress": 40.0, "is_finished": False,
                                    "opts": p[-1]}
                res = h
            elif m == "core.get_torrents_status":
                ids = p[0].get("id")
                res = {h: t for h, t in self.torrents.items() if ids is None or h in ids}
            return httpx.Response(200, json={"id": body["id"], "result": res, "error": None})
        if u.host == "sab":
            q = parse_qs(u.query.decode())
            if q.get("apikey") != ["SKEY"]:
                return httpx.Response(200, json={"status": False, "error": "API Key Incorrect"})
            mode = q["mode"][0]
            if mode == "version":
                return httpx.Response(200, json={"version": "4.3.2"})
            if mode == "get_cats":
                return httpx.Response(200, json={"categories": ["*", "books"]})
            if mode == "addfile":
                nid = f"SABnzbd_nzo_{len(self.sab_adds)}"
                self.sab_adds.append((q.get("cat"), q["nzbname"][0], b"nzb" in req.content))
                self.sab_queue[nid] = {"nzo_id": nid, "status": "Downloading", "percentage": "30", "filename": "x"}
                return httpx.Response(200, json={"status": True, "nzo_ids": [nid]})
            if mode == "queue":
                return httpx.Response(200, json={"queue": {"slots": list(self.sab_queue.values())}})
            if mode == "history":
                return httpx.Response(200, json={"history": {"slots": list(self.sab_hist.values())}})
        return httpx.Response(404)


@pytest.fixture()
def fake():
    return Fake()


@pytest.fixture()
def http(fake):
    return httpx.Client(transport=httpx.MockTransport(fake))


@pytest.fixture()
def setup(env, conn):  # noqa: F811
    _add_works(conn, EXTRA)
    d = make_calibre(env / "calibre")
    db.set_meta(conn, "setting_calibre_path", str(d))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    for k, v in {"prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "PKEY", "deluge_url": "http://deluge:8112",
                 "deluge_password": "secret", "deluge_label": "Ebooks", "sab_url": "http://sab:8080",
                 "sab_api_key": "SKEY", "sab_category": "books"}.items():
        db.set_meta(conn, f"setting_{k}", v)
    monitor.set_monitor(conn, TOLKIEN, "all")
    return conn


def _px(conn, http):  # noqa: F811
    return prowlarr.Prowlarr(**prowlarr.config(conn), client=http)


# --- scoring ------------------------------------------------------------------------------------------

def test_release_scoring(setup, http):
    res = grabs.search_releases(setup, HURIN, _px(setup, http))
    by = {r["title"]: r for r in res["releases"]}
    best = res["releases"][0]
    assert best["title"].endswith("[Retail EPUB]") and best["format"] == "EPUB" and not best["rejected"]
    assert by["Tolkien - The Children of Hurin MP3 Unabridged"]["rejected"] == ["audiobook"]
    assert "looks German" in by["Die Kinder Hurins - Tolkien [GER] epub"]["rejected"]
    assert by["The Children of Hurin - Tolkien.pdf"]["rejected"] == ["no seeders"]
    mobi = by["J R R Tolkien The Children of Hurin mobi"]
    assert not mobi["rejected"] and mobi["format"] == "MOBI"
    # rejected ones sort last; no download URLs leak to the browser
    assert [bool(r["rejected"]) for r in res["releases"]] == sorted(bool(r["rejected"]) for r in res["releases"])
    assert not any(k.startswith("_") for r in res["releases"] for k in r)
    assert res["queries"] == ["Tolkien The Children of Hurin", "The Children of Hurin"]


def test_scoring_details():
    w = {"title": "The Hobbit", "authors": [{"name": "J.R.R. Tolkien"}]}
    p = {"languages": ["eng"], "max_size_mb": 200, "min_seeders": 1}
    ev = lambda t, **k: releases.evaluate({"title": t, "size": 1_000_000, "seeders": 5, "protocol": "torrent",  # noqa
                                           **k}, w, p)
    assert ev("The Hobbit Companion - Tolkien epub")["score"] < ev("Tolkien - The Hobbit epub")["score"]
    assert ev("Antoine de Saint-Exupery - The Hobbit epub")["rejected"] == []     # "de" is not German
    assert ev("Tolkien - The Hobbit (Illustrated) [DE] epub")["rejected"] == ["looks German"]
    assert ev("Tolkien - The Hobbit epub", size=900 * 1024 * 1024)["rejected"] == ["too big (900 MB)"]
    assert ev("Tolkien - The Hobbit [Comic] cbz")["rejected"] == ["comic"]
    assert "author's name (tolkien) isn't in the release name" in ev("The Hobbit epub")["reasons"]
    assert releases.detect_format("Book [EPUB+MOBI+AZW3]") == "EPUB"


# --- grabbing -------------------------------------------------------------------------------------------

def test_grab_torrent_to_deluge_and_follow(setup, http, fake):
    res = grabs.search_releases(setup, HURIN, _px(setup, http))
    best = res["releases"][0]
    dl = clients.Deluge("http://deluge:8112", "secret", http)
    g = grabs.grab(setup, HURIN, best["id"], px=_px(setup, http), deluge=dl)
    assert g["client"] == "deluge" and g["client_id"] == "11" * 20 and g["state"] == "queued"
    assert "label.set_torrent" in fake.deluge_calls and "web.connect" in fake.deluge_calls
    # the book shows as downloading, not wanted
    st = {w["ol_key"]: w["status"] for w in monitor.wanted(setup)}
    assert st[HURIN] == "downloading"
    # progress, then done
    assert grabs.poll(setup, deluge=dl)["completed"] == 0
    assert grabs.get(setup, g["id"])["state"] == "downloading" and grabs.get(setup, g["id"])["progress"] == 40
    fake.torrents["11" * 20].update(is_finished=True, progress=100.0, state="Seeding")
    assert grabs.poll(setup, deluge=dl)["completed"] == 1
    assert grabs.get(setup, g["id"])["state"] == "completed"
    # once Calibre has the book, it's imported
    setup.execute("UPDATE library_books SET work_key=? WHERE calibre_id=5", (HURIN,))
    assert grabs.poll(setup, deluge=dl)["imported"] == 1
    assert grabs.get(setup, g["id"])["state"] == "imported"


def test_grab_magnet_and_redirect(setup, http, fake):
    res = grabs.search_releases(setup, HURIN, _px(setup, http))
    mag = next(r for r in res["releases"] if "(magnet)" in r["title"])
    dl = clients.Deluge("http://deluge:8112", "secret", http)
    g = grabs.grab(setup, HURIN, mag["id"], px=_px(setup, http), deluge=dl)
    assert g["client_id"] == "abcdef0123456789abcdef0123456789abcdef01"
    proxied = next(r for r in res["releases"] if "(proxied magnet)" in r["title"])
    fake.torrents.clear()
    g = grabs.grab(setup, HURIN, proxied["id"], px=_px(setup, http), deluge=dl)
    assert g["client_id"] == "abcdef0123456789abcdef0123456789abcdef01"
    # an indexer that redirects its download link to a magnet; the same torrent again is "already in session"
    fake.redirect_to_magnet = True
    best = res["releases"][0]
    g2 = grabs.grab(setup, HURIN, best["id"], px=_px(setup, http), deluge=dl)
    assert g2["client_id"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert clients.magnet_hash("magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U&dn=x") == \
        base64.b32decode("MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U").hex()


def test_grab_usenet_to_sab(setup, http, fake):
    res = grabs.search_releases(setup, HURIN, _px(setup, http))
    nzb = next(r for r in res["releases"] if r["protocol"] == "usenet")
    sab = clients.Sabnzbd("http://sab:8080", "SKEY", http)
    g = grabs.grab(setup, HURIN, nzb["id"], px=_px(setup, http), sab=sab)
    assert g["client"] == "sabnzbd" and g["client_id"] == "SABnzbd_nzo_0"
    assert fake.sab_adds == [(["books"], nzb["title"], True)]
    grabs.poll(setup, sab=sab)
    assert grabs.get(setup, g["id"])["progress"] == 30
    fake.sab_queue.clear()
    fake.sab_hist["SABnzbd_nzo_0"] = {"nzo_id": "SABnzbd_nzo_0", "status": "Failed", "fail_message": "missing articles",
                                      "name": "x", "storage": None}
    grabs.poll(setup, sab=sab)
    g = grabs.get(setup, g["id"])
    assert g["state"] == "failed" and g["message"] == "missing articles"
    st = {w["ol_key"]: w["status"] for w in monitor.wanted(setup)}
    assert st[HURIN] == "wanted"                         # a failed download makes it wanted again


def test_errors(setup, http):
    with pytest.raises(grabs.GrabError, match="expired"):
        grabs.grab(setup, HURIN, "nope", px=_px(setup, http))
    with pytest.raises(prowlarr.ProwlarrError, match="API key"):
        prowlarr.Prowlarr("http://prowlarr:9696", "wrong", http).test()
    with pytest.raises(clients.ClientError, match="password"):
        clients.Deluge("http://deluge:8112", "bad", http).test()
    with pytest.raises(clients.ClientError, match="API Key Incorrect"):
        clients.Sabnzbd("http://sab:8080", "bad", http).test()
    with pytest.raises(prowlarr.ProwlarrError, match="isn't set up"):
        prowlarr.Prowlarr("", "")
    assert prowlarr.Prowlarr("http://prowlarr:9696", "PKEY", http).test()["indexers"][1]["name"] == "NZBGeek"
    assert clients.Deluge("http://deluge:8112/", "secret", http).test()["label_plugin"] is True


def test_downloaded_file_matches_its_grab(setup, http, env):
    """A file named like the grabbed release is matched to the book even when its own metadata says little."""
    res = grabs.search_releases(setup, HURIN, _px(setup, http))
    dl = clients.Deluge("http://deluge:8112", "secret", http)
    best = res["releases"][0]
    grabs.grab(setup, HURIN, best["id"], px=_px(setup, http), deluge=dl)
    m = ingest.match_file(setup, None, None, f"{best['title']}/book.epub")
    assert m["work_key"] == HURIN and m["match_method"] == "download" and m["match_confidence"] >= ingest.REVIEW
    other = ingest.match_file(setup, None, None, "Something Else/book.epub")
    assert other["work_key"] != HURIN


def test_api(env, setup, monkeypatch, http, fake):
    from app.main import create_app
    monkeypatch.setattr(prowlarr.httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(clients.httpx, "Client", lambda **kw: http)
    with TestClient(create_app()) as c:
        st = c.get("/api/settings").json()
        assert st["prowlarr_api_key_set"] and st["deluge_password_set"] and "prowlarr_api_key" not in st
        assert st["release_max_size_mb"] == 200
        assert c.post("/api/downloads/test", json={"client": "prowlarr"}).json()["ok"]
        assert c.post("/api/downloads/test", json={"client": "sabnzbd"}).json()["version"] == "4.3.2"
        assert c.put("/api/settings", json={"deluge_password": "bad"}).status_code == 200
        bad = c.post("/api/downloads/test", json={"client": "deluge"}).json()
        assert bad["ok"] is False and "password" in bad["message"]
        c.put("/api/settings", json={"deluge_password": "secret"})
        r = c.get(f"/api/releases/{HURIN}").json()
        g = c.post("/api/grabs", json={"work_key": HURIN, "release_id": r["releases"][0]["id"]}).json()
        assert g["state"] == "queued"
        assert c.get("/api/grabs").json()[0]["work_title"] == "The Children of Húrin"
        assert c.post("/api/grabs/refresh").json()["checked"] == 1
        assert c.delete(f"/api/grabs/{g['id']}").json() == {"removed": True}
        assert c.post("/api/grabs", json={"work_key": HURIN, "release_id": "x"}).status_code == 502
        assert c.put("/api/settings", json={"prowlarr_url": "prowlarr"}).status_code == 400


def test_seeding_torrents_are_left_in_place(setup, http, fake, env, monkeypatch):
    """A file from a torrent still in Deluge is imported but not removed, until the torrent leaves Deluge."""
    from app import importer
    from app.config import get_settings
    monkeypatch.setattr(clients.httpx, "Client", lambda **kw: http)
    root = env / "ingest"
    (root / "Tolkien - The Hobbit [EPUB]").mkdir(parents=True)
    f = root / "Tolkien - The Hobbit [EPUB]" / "The Hobbit.epub"
    f.write_bytes(b"PK fake epub")
    loose = root / "Loose Hobbit.epub"
    loose.write_bytes(b"PK another")
    db.set_meta(setup, "setting_ingest_path", str(root))
    for rel in ("Tolkien - The Hobbit [EPUB]/The Hobbit.epub", "Loose Hobbit.epub"):
        setup.execute("INSERT INTO ingest_files(path, size, kind, format, bucket, work_key, match_confidence) "
                      "VALUES(?, 10, 'ebook', 'EPUB', 'matched', 'OL262758W', 95)", (rel,))
    setup.commit()
    fake.torrents["22" * 20] = {"name": "Tolkien - The Hobbit [EPUB]", "state": "Seeding", "progress": 100.0,
                                "is_finished": True}
    p = importer.plan(setup)
    assert {i["kind"] for i in p["items"]} == {"already_have"}            # Calibre has The Hobbit as EPUB
    keys = [i["key"] for i in p["items"]]
    importer.run(setup, get_settings(), _noop, _no, keys=keys, batch=1)
    assert f.exists() and not loose.exists()                            # the seeding one stays, the loose one goes
    p = importer.plan(setup)
    assert [(i["kind"], i["select"]) for i in p["items"]] == [("seeding", False)]
    assert importer.run(setup, get_settings(), _noop, _no, keys=[p["items"][0]["key"]], batch=2)["total"] == 0
    assert f.exists()
    # the torrent is removed from Deluge: now it can go
    fake.torrents.clear()
    p = importer.plan(setup)
    assert [(i["kind"], i["select"]) for i in p["items"]] == [("seeding_done", True)]
    importer.run(setup, get_settings(), _noop, _no, batch=3)
    assert not f.exists() and not f.parent.exists()
    assert setup.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 0


def test_seeding_when_deluge_is_down(setup, env, monkeypatch):
    """Deluge set up but unreachable: Tomearr's own torrent grabs count as still seeding."""
    setup.execute("INSERT INTO grabs(work_key, title, client, client_id, state) "
                  "VALUES('OL100W', 'Tolkien - Children of Hurin', 'deluge', 'x', 'imported')")
    db.set_meta(setup, "setting_deluge_url", "http://127.0.0.1:1")
    names, reached = grabs.seeding_names(setup)
    assert not reached and grabs.is_seeding("Tolkien - Children of Hurin/b.epub", names)
    assert not grabs.is_seeding("Other/b.epub", names)
    db.set_meta(setup, "setting_keep_seeding", "0")
    assert grabs.seeding_names(setup) == (set(), True)
