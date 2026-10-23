import time

import pytest
from fastapi.testclient import TestClient

from app import db, ol_import, search
from app.config import get_settings


def _no(): return False
def _noop(_p): pass


@pytest.fixture()
def conn(env):
    s = get_settings()
    c = db.connect(s.db_path)
    db.init(c)
    ol_import.import_authors(c, s.dump_path("authors"), _noop, _no)
    ol_import.import_works(c, s.dump_path("works"), _noop, _no)
    ol_import.build_indexes(c, _noop)
    yield c
    c.close()


def test_import_skips_junk(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM authors")}
    assert names == {"J.R.R. Tolkien", "William Shakespeare", "Christopher Tolkien"}
    titles = {r["title"] for r in conn.execute("SELECT title FROM works")}
    assert "Orphan with no author" not in titles  # require_author default
    assert "Hamlet" in titles and len(titles) == 5


def test_work_fields_parsed(conn):
    hobbit = conn.execute("SELECT * FROM works WHERE ol_key='OL262758W'").fetchone()
    assert hobbit["first_publish_year"] == 1937 and hobbit["subtitle"] == "There and Back Again"
    lotr = conn.execute("SELECT * FROM works WHERE ol_key='OL27448W'").fetchone()
    assert lotr["cover_id"] == 9255566  # skips the -1 placeholder
    assert lotr["subjects"] == "Fantasy | Middle Earth"
    # both author shapes ({"author": {"key"}} and {"author": "key"}) are linked
    keys = {r[0] for r in conn.execute("SELECT work_key FROM work_authors WHERE author_key='OL26320A'")}
    assert keys == {"OL27448W", "OL262758W", "OL1W"}


def test_author_search_ranks_by_work_count(conn):
    res = search.search_authors(conn, "tolkien")
    assert [r["ol_key"] for r in res][:2] == ["OL26320A", "OL1A"]
    assert res[0]["work_count"] == 3


def test_author_search_matches_alternate_names_and_prefix(conn):
    assert search.search_authors(conn, "John Ronald")[0]["ol_key"] == "OL26320A"
    assert search.search_authors(conn, "shakes")[0]["name"] == "William Shakespeare"


def test_work_search_by_title_and_author(conn):
    assert search.search_works(conn, "hobbit")[0]["title"] == "The Hobbit"
    titles = {w["title"] for w in search.search_works(conn, "shakespeare")}
    assert titles == {"Hamlet", "Macbeth"}
    assert search.search_works(conn, "hobbit tolkien")[0]["authors"][0]["name"] == "J.R.R. Tolkien"


def test_search_survives_hostile_input(conn):
    for q in ['"', "NEAR(", "a OR", "*", "'; DROP TABLE works;--", ""]:
        search.search_works(conn, q)
        search.search_authors(conn, q)
    assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 5


def test_author_page_orders_works_by_year(conn):
    a = search.get_author(conn, "OL26320A")
    years = [w["first_publish_year"] for w in a["works"]]
    assert years == [1937, 1954, None]


def test_reimport_is_idempotent(conn):
    s = get_settings()
    ol_import.import_authors(conn, s.dump_path("authors"), _noop, _no)
    ol_import.import_works(conn, s.dump_path("works"), _noop, _no)
    ol_import.build_indexes(conn, _noop)
    assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM work_authors").fetchone()[0] == 5
    assert len(search.search_works(conn, "hamlet")) == 1


def test_cancel_during_import(env):
    s = get_settings()
    c = db.connect(s.db_path)
    db.init(c)
    old = ol_import.PROGRESS_EVERY
    ol_import.PROGRESS_EVERY = 2
    try:
        with pytest.raises(ol_import.ImportCancelled):
            ol_import.import_authors(c, s.dump_path("authors"), _noop, lambda: True)
    finally:
        ol_import.PROGRESS_EVERY = old


def test_api_full_flow(env):
    from app.main import create_app
    with TestClient(create_app()) as client:
        assert client.get("/api/status").json()["searchable"] is False
        assert client.get("/api/search/works?q=hobbit").json() == []  # no index yet, no crash

        job_ids = client.post("/api/catalog/import", json={}).json()["job_ids"]
        deadline = time.time() + 20
        while time.time() < deadline:
            jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
            if all(jobs[i]["status"] in ("done", "failed") for i in job_ids):
                break
            time.sleep(0.2)
        assert [jobs[i]["status"] for i in job_ids] == ["done", "done"], jobs

        st = client.get("/api/status").json()
        assert st["searchable"] and st["counts"] == {"authors": 3, "works": 5}
        assert client.get("/api/search/authors?q=tolkien").json()[0]["ol_key"] == "OL26320A"
        assert client.get("/api/authors/OL9388A").json()["work_count"] == 2
        assert client.get("/api/authors/NOPE").status_code == 404


def test_failed_job_skips_dependents(env):
    from app.main import create_app
    (env / "data" / "dumps" / "ol_dump_works.txt.gz").unlink()
    with TestClient(create_app()) as client:
        job_ids = client.post("/api/catalog/import", json={"kinds": ["works"]}).json()["job_ids"]
        deadline = time.time() + 10
        while time.time() < deadline:
            jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
            if jobs[job_ids[1]]["status"] != "queued":
                break
            time.sleep(0.1)
        assert jobs[job_ids[0]]["status"] == "failed"
        assert "not found" in jobs[job_ids[0]]["message"]
        assert jobs[job_ids[1]]["status"] == "skipped"
