"""Phase 2: editions, ISBNs, popularity ranking, language filter, schema migrations."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import db, isbn, ol_import, search
from app.config import get_settings
from test_catalog import _no, _noop, conn  # noqa: F401  (shared fixture)


# --- ISBN normalization ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("0261102214", "9780261102217"),
    ("0-261-10221-4", "9780261102217"),
    ("978-0-261-10221-7", "9780261102217"),
    ("080442957X", "9780804429573"),
    ("080442957x", "9780804429573"),
    ("0547928228", None),          # bad ISBN-10 checksum
    ("9780261102218", None),       # bad ISBN-13 checksum
    ("1234567890123", None),       # not a 978/979 prefix
    ("X802611022", None),          # X only allowed as the check digit
    ("", None), (None, None), ("hobbit", None),
])
def test_to_isbn13(raw, expected):
    assert isbn.to_isbn13(raw) == expected


def test_isbn_query_detection():
    assert isbn.from_query("ISBN 0-261-10221-4") == "9780261102217"
    assert isbn.from_query("isbn-13: 9780261102217") == "9780261102217"
    assert isbn.from_query("9780261102217") == "9780261102217"
    assert isbn.from_query("the hobbit") is None
    assert isbn.from_query("1984") is None


def test_norm_format():
    assert ol_import.norm_format("E-book") == "ebook"
    assert ol_import.norm_format("Kindle Edition") == "ebook"
    assert ol_import.norm_format("Audio CD") == "audio"
    assert ol_import.norm_format("Mass Market Paperback") == "print"
    assert ol_import.norm_format("") is None and ol_import.norm_format(None) is None


# --- import ------------------------------------------------------------------

def test_editions_imported(conn):
    eds = {r["ol_key"]: dict(r) for r in conn.execute("SELECT * FROM editions")}
    assert set(eds) == {f"OL{i}M" for i in range(1, 9)}  # OL9M has no work and is skipped
    e = eds["OL1M"]
    assert (e["work_key"], e["language"], e["publisher"], e["publish_year"], e["pages"], e["format"]) == \
        ("OL262758W", "eng", "HarperCollins", 1999, 310, "print")
    assert eds["OL2M"]["format"] == "ebook" and eds["OL8M"]["format"] == "audio"
    # ISBN-10 and ISBN-13 of the same book collapse to one row; bad checksums are dropped
    rows = sorted((r["isbn"], r["edition_key"]) for r in conn.execute("SELECT * FROM isbns"))
    assert ("9780618002214", "OL2M") in rows and len([r for r in rows if r[1] == "OL2M"]) == 1
    assert not [r for r in rows if r[1] == "OL3M"]


def test_work_counts_languages_and_signals(conn):
    w = conn.execute("SELECT * FROM works WHERE ol_key='OL262758W'").fetchone()
    assert w["edition_count"] == 4 and w["ebook_count"] == 1
    assert set(w["languages"].strip(",").split(",")) == {"eng", "fre"}
    assert conn.execute("SELECT languages FROM works WHERE ol_key='OL6W'").fetchone()[0] is None  # unknown
    s = conn.execute("SELECT * FROM work_signals WHERE work_key='OL262758W'").fetchone()
    assert (s["rating_sum"], s["rating_count"]) == (14, 3)
    assert (s["want_count"], s["reading_count"], s["read_count"]) == (1, 1, 1)
    assert conn.execute("SELECT rating_count FROM work_signals WHERE work_key='OL7W'").fetchone()[0] == 1
    # popularity = editions + 3*ratings + 2*read + reading + want = 4 + 9 + 2 + 1 + 1
    assert w["popularity"] == 17
    assert w["pop_rank"] == 1


def test_signal_reimport_replaces_not_doubles(conn):
    s = get_settings()
    ol_import.import_signals(conn, s.dump_path("ratings"), "ratings", _noop, _no)
    ol_import.import_signals(conn, s.dump_path("reading-log"), "reading-log", _noop, _no)
    ol_import.import_editions(conn, s.dump_path("editions"), _noop, _no)
    ol_import.build_indexes(conn, _noop)
    sig = conn.execute("SELECT * FROM work_signals WHERE work_key='OL262758W'").fetchone()
    assert sig["rating_count"] == 3 and sig["want_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 8
    assert conn.execute("SELECT edition_count FROM works WHERE ol_key='OL262758W'").fetchone()[0] == 4


def test_lite_mode(env):
    s = get_settings()
    c = db.connect(s.db_path)
    db.init(c)
    ol_import.import_editions(c, s.dump_path("editions"), _noop, _no, isbn_only=True)
    assert {r[0] for r in c.execute("SELECT ol_key FROM editions")} == {"OL1M", "OL2M", "OL6M", "OL7M"}
    ol_import.import_editions(c, s.dump_path("editions"), _noop, _no, languages=["eng"])
    keys = {r[0] for r in c.execute("SELECT ol_key FROM editions")}
    assert "OL4M" not in keys and "OL7M" not in keys  # French
    assert "OL5M" in keys                            # no language listed: kept
    c.close()


def test_import_reports_byte_progress(env):
    s = get_settings()
    c = db.connect(s.db_path)
    db.init(c)
    seen = []
    ol_import.import_editions(c, s.dump_path("editions"), seen.append, _no)
    last = seen[-1]
    assert last["phase"] == "editions" and last["total"] > 0 and last["bytes"] == last["total"]
    c.close()


# --- search ------------------------------------------------------------------

def test_hobbit_ranks_tolkien_first(conn):
    res = search.search_works(conn, "hobbit")
    assert res[0]["ol_key"] == "OL262758W"
    assert res[0]["authors"][0]["name"] == "J.R.R. Tolkien"
    order = [r["ol_key"] for r in res]
    # exact-title decoy beats the partial-title companion despite the companion having a rating
    assert order.index("OL6W") < order.index("OL7W")


def test_popularity_breaks_text_ties(conn):
    # "shakespeare" matches only the author column, so popularity alone orders the results
    assert len(search.search_works(conn, "shakespeare")) == 2
    res = search.search_works(conn, "tolkien")
    assert res[0]["ol_key"] == "OL262758W"


def test_language_filter(conn):
    assert search.search_works(conn, "prince", lang="eng") == []
    assert search.search_works(conn, "prince", lang="fre")[0]["ol_key"] == "OL8W"
    assert search.search_works(conn, "prince", lang="all")[0]["ol_key"] == "OL8W"
    # works with no known language are not hidden by the filter
    assert "OL3W" in {w["ol_key"] for w in search.search_works(conn, "macbeth", lang="eng")}


def test_isbn_lookup(conn):
    for q in ("0261102214", "978-0-261-10221-7"):
        r = search.lookup_isbn(conn, q)
        assert r["works"][0]["ol_key"] == "OL262758W"
        assert r["editions"][0]["ol_key"] == "OL1M"
        assert r["works"][0]["authors"][0]["name"] == "J.R.R. Tolkien"
    assert search.lookup_isbn(conn, "9780000000002")["works"] == []
    assert search.lookup_isbn(conn, "nope") is None


def test_work_detail(conn):
    w = search.get_work(conn, "OL262758W")
    assert w["ratings"] == {"count": 3, "average": 4.67}
    assert w["readers"] == {"want": 1, "reading": 1, "read": 1}
    assert w["editions"][0]["format"] == "ebook"  # ebooks listed first
    assert {e["ol_key"]: e["isbns"] for e in w["editions"]}["OL1M"] == ["9780261102217"]
    assert sorted(w["languages"]) == ["eng", "fre"]
    assert search.get_work(conn, "OL404W") is None


# --- migrations ----------------------------------------------------------------

def test_migrates_phase1_database(env):
    """A database created by Phase 1 (no schema_version, old works table, rowid FTS) upgrades in place."""
    s = get_settings()
    c = sqlite3.connect(s.db_path)
    c.executescript(db.SCHEMA_V1)
    c.execute("INSERT INTO works(ol_key, title) VALUES('OL1W', 'Old Book')")
    c.execute("CREATE VIRTUAL TABLE works_fts USING fts5(title, authors, content='', columnsize=0)")
    c.execute("CREATE VIRTUAL TABLE authors_fts USING fts5(name, alt, content='', columnsize=0)")
    c.execute("INSERT INTO works_fts(rowid, title, authors) SELECT rowid, title, '' FROM works")
    c.commit()
    c.close()

    c = db.connect(s.db_path)
    db.init(c)
    assert db.schema_version(c) == db.SCHEMA_VERSION
    cols = {r[1] for r in c.execute("PRAGMA table_info(works)")}
    assert {"edition_count", "popularity", "pop_rank", "languages"} <= cols
    # old index still searchable before a rebuild (joins on rowid until build_indexes switches it)
    assert search.search_works(c, "old")[0]["ol_key"] == "OL1W"
    db.init(c)  # idempotent
    assert db.schema_version(c) == db.SCHEMA_VERSION
    c.close()


# --- API -----------------------------------------------------------------------

def _wait(client, ids, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
        if all(jobs[i]["status"] not in ("queued", "running") for i in ids):
            return jobs
        time.sleep(0.1)
    raise AssertionError("jobs did not finish")


def test_api_phase2(env):
    from app.main import create_app
    with TestClient(create_app()) as client:
        ids = client.post("/api/catalog/import", json={}).json()["job_ids"]
        jobs = _wait(client, ids)
        assert [jobs[i]["status"] for i in ids] == ["done", "done"], jobs

        assert client.get("/api/settings").json()["language"] == "eng"
        assert client.get("/api/search/works?q=prince").json() == []
        assert client.put("/api/settings", json={"language": "all"}).json()["language"] == "all"
        assert client.get("/api/search/works?q=prince").json()[0]["ol_key"] == "OL8W"
        assert client.get("/api/search/works?q=prince&lang=eng").json() == []
        assert client.put("/api/settings", json={"language": "english!"}).status_code == 400
        assert client.get("/api/search/works?q=x&lang=bad'").status_code == 400

        r = client.get("/api/isbn/0-261-10221-4").json()
        assert r["isbn"] == "9780261102217" and r["works"][0]["ol_key"] == "OL262758W"
        assert client.get("/api/isbn/12345").status_code == 400
        assert client.get("/api/works/OL262758W").json()["edition_count"] == 4
        assert client.get("/api/works/OL404W").status_code == 404

        st = client.get("/api/status").json()
        assert st["schema_version"] == db.SCHEMA_VERSION and st["editions_lite"] == {"languages": [], "isbn_only": False}
        assert {d["kind"] for d in st["dumps"]} == {"authors", "works", "editions", "ratings", "reading-log"}


def test_api_lite_import_params(env):
    from app.main import create_app
    with TestClient(create_app()) as client:
        ids = client.post("/api/catalog/import", json={
            "kinds": ["authors", "works", "editions"], "edition_languages": [" ENG "], "isbn_only": True,
        }).json()["job_ids"]
        jobs = _wait(client, ids)
        assert jobs[ids[0]]["params"]["edition_languages"] == ["eng"]
        st = client.get("/api/status").json()
        assert st["counts"]["editions"] == 3  # OL1M, OL2M, OL6M: English with ISBNs
        assert st["editions_lite"] == {"languages": ["eng"], "isbn_only": True}
