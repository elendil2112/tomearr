"""Phases 8-9: search for a wanted book, send the release you pick to a download client, follow it.

A grab moves through: queued -> downloading -> completed -> imported (or failed / missing). "completed" means the
client finished; the file then shows up in the ingest folder, gets matched and imported like any other file, and the
grab is marked imported once Calibre has the book.
"""
import time

import orjson

from . import clients, db, monitor, prowlarr, releases, search

DEFAULTS = {"prowlarr_url": "", "deluge_url": "", "deluge_label": "", "deluge_path": "", "sab_url": "",
            "sab_category": "", "release_max_size_mb": "200", "release_min_seeders": "1", "keep_seeding": "1"}
SECRETS = ("prowlarr_api_key", "deluge_password", "sab_api_key")
ACTIVE = ("queued", "downloading", "completed")


class GrabError(Exception):
    pass


def prefs(conn) -> dict:
    g = lambda k: db.get_meta(conn, f"setting_{k}", DEFAULTS[k])  # noqa: E731
    return {"languages": monitor.profile(conn)["languages"], "max_size_mb": int(g("release_max_size_mb") or 0),
            "min_seeders": int(g("release_min_seeders") or 0)}


def _work(conn, work_key: str) -> dict:
    w = search.get_work(conn, work_key, edition_limit=0)
    if not w:
        raise KeyError(work_key)
    return w


def search_releases(conn, work_key: str, px: prowlarr.Prowlarr | None = None) -> dict:
    w = _work(conn, work_key)
    px = px or prowlarr.Prowlarr(**prowlarr.config(conn))
    t0 = time.time()
    raw, queries = [], releases.query_for(w)
    for q in queries:
        rows = px.search(q)
        raw += rows
        if len([r for r in rows if r.get("title")]) >= 25:    # the precise search found plenty
            break
    ranked = releases.rank(raw, w, prefs(conn))
    prowlarr.remember(ranked)
    return {"work_key": work_key, "title": w["title"], "queries": queries, "took": round(time.time() - t0, 1),
            "total": len(ranked), "releases": [releases.public(r) for r in ranked],
            "active": [dict(g) for g in active(conn, work_key)]}


def active(conn, work_key: str | None = None) -> list:
    sql = f"SELECT * FROM grabs WHERE state IN ({','.join('?' * len(ACTIVE))})"
    args = list(ACTIVE)
    if work_key:
        sql += " AND work_key=?"
        args.append(work_key)
    return conn.execute(sql + " ORDER BY id DESC", args).fetchall()


def active_keys(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT work_key FROM grabs WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE)}


def grab(conn, work_key: str, release_id: str, px=None, deluge=None, sab=None) -> dict:
    rel = prowlarr.recall(release_id)
    if not rel:
        raise GrabError("that search result has expired; search again")
    _work(conn, work_key)
    px = px or prowlarr.Prowlarr(**prowlarr.config(conn))
    note = None
    if rel["protocol"] == "torrent":
        # Either link may be a Prowlarr address that redirects to a .torrent or a magnet; fetch() resolves both.
        # Try the download link first, then the magnet link.
        links = [u for u in (rel.get("_download_url"), rel.get("_magnet_url")) if u]
        if not links:
            raise GrabError("the indexer gave no download link")
        kind = payload = None
        for i, link in enumerate(links):
            try:
                kind, payload = px.fetch(link)
                break
            except prowlarr.ProwlarrError:
                if i == len(links) - 1:
                    raise
        dl = deluge or clients.deluge(conn)
        cid, note = dl.add(kind, payload, rel["title"], label=clients.setting(conn, "deluge_label"),
                           path=clients.setting(conn, "deluge_path"))
        client = "deluge"
    elif rel["protocol"] == "usenet":
        if not rel.get("_download_url"):
            raise GrabError("the indexer gave no NZB link")
        kind, payload = px.fetch(rel["_download_url"])
        if kind != "file":
            raise GrabError("expected an NZB file")
        sb = sab or clients.sabnzbd(conn)
        cid = sb.add(payload, rel["title"], clients.setting(conn, "sab_category"))
        client = "sabnzbd"
    else:
        raise GrabError(f"don't know how to download a {rel['protocol']} release")
    cur = conn.execute(
        "INSERT INTO grabs(work_key, title, indexer, protocol, format, size, client, client_id, message, release_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (work_key, rel["title"], rel["indexer"], rel["protocol"], rel["format"], rel["size"], client, cid, note,
         orjson.dumps(releases.public(rel)).decode()))
    conn.commit()
    return get(conn, cur.lastrowid)


def get(conn, grab_id: int) -> dict:
    r = conn.execute("SELECT * FROM grabs WHERE id=?", (grab_id,)).fetchone()
    if not r:
        raise KeyError(grab_id)
    return _row(r)


def _row(r) -> dict:
    d = dict(r)
    d.pop("release_json", None)
    return d


def history(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT g.*, w.title AS work_title FROM grabs g LEFT JOIN works w ON w.ol_key = g.work_key "
                        "ORDER BY (g.state IN ('queued','downloading','completed')) DESC, g.id DESC LIMIT ?",
                        (limit,)).fetchall()
    return [_row(r) for r in rows]


def remove(conn, grab_id: int) -> bool:
    """Forget a grab (the download itself stays in the client)."""
    cur = conn.execute("UPDATE grabs SET state='removed', updated_at=datetime('now') WHERE id=? "
                       "AND state NOT IN ('imported','removed')", (grab_id,))
    conn.commit()
    return cur.rowcount > 0


def poll(conn, deluge=None, sab=None) -> dict:
    """Ask the clients how active grabs are doing. Returns counts; 'completed' > 0 means scan the ingest folder."""
    rows = active(conn)
    if not rows:
        return {"checked": 0, "completed": 0, "imported": 0}
    owned = {r[0] for r in conn.execute("SELECT DISTINCT work_key FROM library_books WHERE work_key IS NOT NULL")}
    done = imported = 0
    by_client: dict[str, list] = {}
    for r in rows:
        if r["work_key"] in owned:
            conn.execute("UPDATE grabs SET state='imported', progress=100, updated_at=datetime('now'), "
                         "finished_at=coalesce(finished_at, datetime('now')) WHERE id=?", (r["id"],))
            imported += 1
        elif r["state"] != "completed":
            by_client.setdefault(r["client"], []).append(r)
    for name, items in by_client.items():
        try:
            c = deluge if name == "deluge" else sab
            c = c or (clients.deluge(conn) if name == "deluge" else clients.sabnzbd(conn))
            st = c.status([i["client_id"] for i in items])
        except clients.ClientError as exc:
            for i in items:
                conn.execute("UPDATE grabs SET message=?, updated_at=datetime('now') WHERE id=?", (str(exc), i["id"]))
            continue
        for i in items:
            s = st.get(i["client_id"]) or st.get((i["client_id"] or "").lower())
            if s is None:
                conn.execute("UPDATE grabs SET state='missing', message=?, updated_at=datetime('now') WHERE id=?",
                             (f"no longer in {name}", i["id"]))
                continue
            if s["state"] == "completed":
                done += 1
            conn.execute("UPDATE grabs SET state=?, progress=?, message=?, updated_at=datetime('now'), "
                         "finished_at=CASE WHEN ? IN ('completed','failed') THEN datetime('now') END WHERE id=?",
                         (s["state"], s["progress"], s["message"], s["state"], i["id"]))
    conn.commit()
    return {"checked": len(rows), "completed": done, "imported": imported}


# --- seeding -----------------------------------------------------------------------------------------------

def _plain(s: str) -> str:
    import re
    from . import matcher
    return re.sub(r"[^a-z0-9]+", "", matcher.fold(s))


def seeding_names(conn, deluge=None) -> tuple[set[str], bool]:
    """Names of every torrent still in Deluge (Tomearr's grabs and anything you added yourself), simplified.

    Returns (names, reached). When Deluge is set up but can't be reached, Tomearr's own torrent grabs are assumed
    to still be seeding, so nothing of theirs is deleted on a guess.
    """
    if db.get_meta(conn, "setting_keep_seeding", DEFAULTS["keep_seeding"]) != "1":
        return set(), True
    if not clients.setting(conn, "deluge_url"):
        return set(), True
    try:
        d = deluge or clients.deluge(conn)
        d.connect()
        res = d._call("core.get_torrents_status", {}, ["name"]) or {}
        return {_plain(t.get("name") or "") for t in res.values()} - {""}, True
    except clients.ClientError:
        rows = conn.execute("SELECT title FROM grabs WHERE client='deluge' AND state != 'removed'")
        names = {_plain(r[0]) for r in rows}
        for r in conn.execute("SELECT DISTINCT json_extract(release_json, '$.title') FROM grabs WHERE client='deluge'"):
            if r[0]:
                names.add(_plain(r[0]))
        return names - {""}, False


def is_seeding(relpath: str, names: set[str]) -> bool:
    """Is this ingest file part of a torrent that's still in Deluge? (any folder in its path, or the file itself)"""
    if not names:
        return False
    parts = relpath.split("/")
    return any(_plain(p) in names for p in parts[:-1]) or _plain(parts[-1]) in names
