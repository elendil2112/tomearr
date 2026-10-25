"""Phase 7: monitored authors, the metadata profile and the wanted list."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, library, monitor
from conftest import make_calibre
from test_catalog import _no, _noop, conn  # noqa: F401  (shared fixture)

TOLKIEN = "OL26320A"
YEAR = datetime.now(timezone.utc).year

# (key, title, year, editions, languages)
EXTRA = [
    ("OL100W", "The Children of Húrin", 2007, 40, ",eng,ger,"),
    ("OL101W", "Unfinished Tales", 1980, 25, ",eng,"),
    ("OL102W", "Study Guide: The Hobbit", 2010, 3, ",eng,"),
    ("OL103W", "The Lord of the Rings Boxed Set", 1999, 12, ",eng,"),
    ("OL104W", "Le Seigneur des Anneaux", 1972, 9, ",fre,"),
    ("OL105W", "Letter to a fan", 1960, 1, ",eng,"),
    ("OL106W", "The Fall of Númenor", YEAR - 1, 1, ",eng,"),       # new: one edition is enough
    ("OL107W", "Beren and Lúthien", 2017, 15, ",eng,"),
]


def _add_works(conn, rows, author=TOLKIEN):  # noqa: F811
    for key, title, year, eds, langs in rows:
        conn.execute("INSERT INTO works(ol_key, title, first_publish_year, edition_count, languages) "
                     "VALUES(?,?,?,?,?)", (key, title, year, eds, langs))
        conn.execute("INSERT INTO work_authors(work_key, author_key) VALUES(?,?)", (key, author))
    conn.commit()


@pytest.fixture()
def lib(env, conn):  # noqa: F811
    _add_works(conn, EXTRA)
    d = make_calibre(env / "calibre")
    db.set_meta(conn, "setting_calibre_path", str(d))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    return d


def _status(view):
    return {w["ol_key"]: w["status"] for w in view["works"]}


def test_profile_filter():
    prof = {"languages": ["eng"], "min_editions": 2, "exclude_collections": True}
    p = lambda **w: monitor.passes({"title": "X", "edition_count": 5, "languages": ",eng,", **w}, prof, 2026)  # noqa
    assert p() == (True, None)
    assert p(languages=",fre,")[0] is False
    assert p(languages=None)[0] is True                                   # many old records don't say
    assert p(edition_count=1)[0] is False
    assert p(edition_count=1, first_publish_year=2025)[0] is True         # too new to have many editions
    for t in ("The Hobbit Box Set", "Complete Works", "Books 1-3", "SparkNotes: Hamlet", "Summary of Dune",
              "The Collected Works of X", "Narnia Omnibus"):
        assert p(title=t)[0] is False, t
    assert p(title="The Collector")[0] is True                            # word boundaries, not substrings
    assert monitor.passes({"title": "X", "edition_count": 5, "languages": ",fre,"},
                          {**prof, "languages": []}, 2026)[0] is True    # blank = any language


def test_tolkien_wanted_list(conn, lib):  # noqa: F811
    """Acceptance: monitoring Tolkien gives a wanted list that is right against what Calibre already has."""
    assert monitor.wanted(conn) == []                                     # starts empty
    v = monitor.author_view(conn, TOLKIEN)
    assert v["monitor"] is None and set(_status(v).values()) <= {"owned", "unmonitored"}

    monitor.set_monitor(conn, TOLKIEN, "all")
    s = _status(monitor.author_view(conn, TOLKIEN))
    assert s["OL262758W"] == "owned" and s["OL27448W"] == "owned" and s["OL1W"] == "owned"   # in Calibre
    assert s["OL100W"] == s["OL101W"] == s["OL107W"] == s["OL106W"] == "wanted"
    assert s["OL102W"] == s["OL103W"] == s["OL104W"] == s["OL105W"] == "filtered"
    wanted = [w["ol_key"] for w in monitor.wanted(conn)]
    assert sorted(wanted) == ["OL100W", "OL101W", "OL106W", "OL107W"]
    assert all(w["authors"][0]["name"] == "J.R.R. Tolkien" for w in monitor.wanted(conn))

    # ignore one, force-want a filtered one
    monitor.set_work(conn, "OL101W", "ignored")
    monitor.set_work(conn, "OL105W", "wanted")
    wanted = {w["ol_key"] for w in monitor.wanted(conn)}
    assert "OL101W" not in wanted and "OL105W" in wanted
    monitor.set_work(conn, "OL101W", None)
    assert "OL101W" in {w["ol_key"] for w in monitor.wanted(conn)}

    counts = monitor.monitored(conn)[0]["counts"]
    assert counts["owned"] == 3 and counts["wanted"] == 5 and counts["filtered"] == 3


def test_future_and_selected(conn, lib):  # noqa: F811
    monitor.set_monitor(conn, TOLKIEN, "future")
    assert monitor.wanted(conn) == []                                      # everything existing is 'before'
    _add_works(conn, [("OL200W", "The Nature of Middle-earth", YEAR, 3, ",eng,")])
    assert [w["ol_key"] for w in monitor.wanted(conn)] == ["OL200W"]
    # switching away and back to future resets the baseline
    monitor.set_monitor(conn, TOLKIEN, "selected")
    assert monitor.wanted(conn) == []
    monitor.set_work(conn, "OL107W", "wanted")
    assert [w["ol_key"] for w in monitor.wanted(conn)] == ["OL107W"]
    monitor.remove_monitor(conn, TOLKIEN)
    assert [w["ol_key"] for w in monitor.wanted(conn)] == ["OL107W"]      # single wanted books stay wanted


def test_profile_settings_change_results(conn, lib):  # noqa: F811
    monitor.set_monitor(conn, TOLKIEN, "all")
    db.set_meta(conn, "setting_profile_languages", "")
    db.set_meta(conn, "setting_profile_exclude_collections", "0")
    s = _status(monitor.author_view(conn, TOLKIEN))
    assert s["OL104W"] == s["OL102W"] == s["OL103W"] == "wanted"
    assert s["OL105W"] == "filtered"                                       # still one edition


def test_in_ingest_status(conn, lib):  # noqa: F811
    monitor.set_monitor(conn, TOLKIEN, "all")
    conn.execute("INSERT INTO ingest_files(path, size, mtime_ns, kind, bucket, work_key) "
                 "VALUES('Unfinished Tales.epub', 1, 1, 'ebook', 'matched', 'OL101W')")
    conn.commit()
    s = {w["ol_key"]: w["status"] for w in monitor.wanted(conn)}
    assert s["OL101W"] == "in_ingest"
    assert "OL101W" not in {w["ol_key"] for w in monitor.wanted(conn, include_waiting=False)}


def test_new_releases(conn, lib):  # noqa: F811
    monitor.set_monitor(conn, TOLKIEN, "all")
    assert [w["ol_key"] for w in monitor.new_releases(conn, 2)] == ["OL106W"]


def test_api(env, lib):
    from app.main import create_app
    with TestClient(create_app()) as c:
        assert c.put(f"/api/monitor/authors/{TOLKIEN}", json={"monitor": "bogus"}).status_code == 400
        assert c.put("/api/monitor/authors/OL0A", json={"monitor": "all"}).status_code == 404
        assert c.put(f"/api/monitor/authors/{TOLKIEN}", json={"monitor": "all"}).json()["monitor"] == "all"
        a = c.get(f"/api/authors/{TOLKIEN}").json()
        assert a["monitor"]["monitor"] == "all" and a["counts"]["wanted"] == 4
        hobbit = next(w for w in a["works"] if w["ol_key"] == "OL262758W")
        assert hobbit["status"] == "owned" and hobbit["owned"]
        assert len(c.get("/api/wanted").json()) == 4
        assert c.get("/api/works/OL101W").json()["monitor_status"]["status"] == "wanted"
        assert c.get("/api/works/OL102W").json()["monitor_status"]["status"] == "filtered"
        assert c.put("/api/monitor/works/OL100W", json={"state": "ignored"}).status_code == 200
        assert len(c.get("/api/wanted", params={"q": "tolkien beren"}).json()) == 1
        assert c.get("/api/monitor/authors").json()[0]["name"] == "J.R.R. Tolkien"
        assert [w["ol_key"] for w in c.get("/api/wanted/new").json()] == ["OL106W"]
        r = c.put("/api/settings", json={"profile_languages": "eng, Spa", "profile_min_editions": 1})
        assert r.status_code == 200
        st = c.get("/api/settings").json()
        assert st["profile_languages"] == "eng,spa" and st["profile_exclude_collections"] is True
        assert c.put("/api/settings", json={"profile_languages": "english"}).status_code == 400
        assert c.delete(f"/api/monitor/authors/{TOLKIEN}").json() == {"removed": True}
        assert c.get("/api/wanted").json() == []
