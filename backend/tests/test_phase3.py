"""Phase 3: read-only Calibre mirror, matching owned books to catalog works."""
import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import calibre, db, library, matcher
from app.config import get_settings
from conftest import make_calibre
from test_catalog import _no, _noop, conn  # noqa: F401  (shared fixture)


def _hash_dir(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir()) if p.is_file()}


@pytest.fixture()
def lib(env, conn):  # noqa: F811
    d = make_calibre(env / "calibre")
    db.set_meta(conn, "setting_calibre_path", str(d))
    return d


def _sync(conn, env, **kw):  # noqa: F811
    return library.sync(conn, env / "config" / "cache", _noop, _no, **kw)


def _row(conn, cid):  # noqa: F811
    return dict(conn.execute("SELECT * FROM library_books WHERE calibre_id=?", (cid,)).fetchone())


# --- matcher -------------------------------------------------------------------

def test_author_similarity():
    assert matcher.author_similarity("J. R. R. Tolkien", "J.R.R. Tolkien") == 100
    assert matcher.author_similarity("Tolkien, J.R.R.", "J.R.R. Tolkien") == 100
    assert matcher.author_similarity("John Tolkien", "J.R.R. Tolkien") == 92
    assert matcher.author_similarity("Christopher Tolkien", "J.R.R. Tolkien") == 80
    assert matcher.author_similarity("William Shakespeare", "J.R.R. Tolkien") < 50


def test_title_normalization():
    assert matcher.norm_title("The Silmarillion (Middle-earth #3)") == "silmarillion"
    assert matcher.main_title("The Hobbit: or There and Back Again") == "The Hobbit"
    assert matcher.norm_title("Pride & Prejudice: A Novel") == "pride and prejudice"


def test_title_similarity_subtitles():
    w = lambda t: {"title": t, "subtitle": None}  # noqa: E731
    assert matcher.title_similarity("Hamlet, Prince of Denmark", w("Hamlet")) >= 85
    assert matcher.title_similarity("The Hobbit (Illustrated)", w("The Hobbit")) == 100
    assert matcher.title_similarity("Dune Messiah", w("Dune")) < 60   # a different book, not a subtitle
    assert matcher.title_similarity("The Hobbit Companion", w("The Hobbit")) < 80


def test_match_prefers_real_work_over_decoys(conn):  # noqa: F811
    m = matcher.match_book(conn, "The Hobbit", ["J.R.R. Tolkien"])
    assert m.work_key == "OL262758W" and m.method == "title_author" and m.confidence >= 90
    # same title, different author: the fan's work wins
    assert matcher.match_book(conn, "The Hobbit", ["Hobbit Fan"]).work_key == "OL6W"


def test_wrong_isbn_falls_back_to_title_and_flags_it(conn):  # noqa: F811
    # Calibre says this is The Hobbit's ISBN, but the title and author say Hamlet: trust the text, flag the ISBN.
    m = matcher.match_book(conn, "Hamlet", ["William Shakespeare"], isbn="9780261102217")
    assert m.work_key == "OL2W" and m.method == "title_author" and m.issues == ["isbn_mismatch"]
    # nothing matches the text either: unmatched, with the ISBN's work offered as a candidate
    m = matcher.match_book(conn, "Completely Different Book", ["Someone"], isbn="9780261102217")
    assert m.work_key is None and m.issues == ["isbn_mismatch"]
    assert "OL262758W" in [c["ol_key"] for c in m.candidates]


def test_isbn_checks_edition_titles_too(conn):  # noqa: F811
    # "Bilbo le Hobbit" is the title of the French edition (OL4M), not of the work. Give it an ISBN.
    conn.execute("INSERT INTO isbns(isbn, edition_key) VALUES('9782253049418', 'OL4M')")
    m = matcher.match_book(conn, "Bilbo le Hobbit", ["J.R.R. Tolkien"], isbn="9782253049418")
    assert m.work_key == "OL262758W" and m.method == "isbn" and m.confidence >= 94 and not m.issues


def test_edition_title_fallback(conn):  # noqa: F811
    # No ISBN, and no work is called this, but one of Tolkien's editions is (a translation).
    m = matcher.match_book(conn, "Bilbo le Hobbit", ["Tolkien| J.R.R."])
    assert m.work_key == "OL262758W" and m.method == "edition_title" and 60 <= m.confidence < 90


def test_same_title_different_author_is_not_a_match(conn):  # noqa: F811
    m = matcher.match_book(conn, "The Hobbit Companion", ["John Schember"])
    assert m.work_key is None
    assert m.candidates and m.candidates[0]["ol_key"] == "OL7W"  # still offered for a manual fix


def test_messy_calibre_title(conn):  # noqa: F811
    m = matcher.match_book(conn, "Middle-earth Vol-3- The Silmarillion J.R.R Tolkien", ["Tolkien| J.R.R."])
    assert m.work_key == "OL1W" and m.confidence >= 90


def test_clean_author_names():
    assert matcher.split_authors("Tolkien| J.R.R.") == ["J.R.R. Tolkien"]
    assert matcher.split_authors("Neil Gaiman (Editor)") == ["Neil Gaiman"]
    assert matcher.split_authors('“Stephen  King”') == ["Stephen King"]
    assert matcher.split_authors("King, Stephen") == ["Stephen King"]
    assert matcher.split_authors("Terry Pratchett & Neil Gaiman") == ["Terry Pratchett", "Neil Gaiman"]
    assert matcher.split_authors("Tolkien, J.R.R.; Tolkien, Christopher") == ["J.R.R. Tolkien", "Christopher Tolkien"]
    assert matcher.split_authors("Unknown") == [] and matcher.split_authors("") == []
    assert matcher.split_authors("Simon and Garfunkel") == ["Simon and Garfunkel"]
    assert matcher.split_authors("Martin Luther King, Jr.") == ["Martin Luther King, Jr."]
    assert matcher.author_similarity("Tolkien| J.R.R.", "J.R.R. Tolkien") == 100
    assert matcher.author_similarity("Martin Luther King Jr.", "Martin Luther King") == 100
    assert matcher.author_similarity("García Márquez, Gabriel", "Gabriel Garcia Marquez") == 100


def test_no_match(conn):  # noqa: F811
    m = matcher.match_book(conn, "My Grocery List 2019", ["Joe Nobody"])
    assert m.work_key is None and m.method is None


# --- snapshot -------------------------------------------------------------------

def test_snapshot_copies_without_touching_calibre(env):
    d = make_calibre(env / "calibre")
    before = _hash_dir(d)
    snap = calibre.snapshot(d, env / "config" / "cache")
    assert snap.parent == env / "config" / "cache"
    assert calibre.summary(snap)["books"] == 6
    assert _hash_dir(d) == before
    assert sorted(p.name for p in d.iterdir()) == ["metadata.db"]  # no -wal/-shm/-journal left behind


def test_snapshot_includes_wal_content(env):
    d = env / "walcal"
    make_calibre(d)
    c = sqlite3.connect(d / "metadata.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO books(id, title) VALUES(99, 'Only In The WAL')")
    c.commit()  # keep the connection open so the WAL isn't checkpointed away
    try:
        assert (d / "metadata.db-wal").exists()
        snap = calibre.snapshot(d, env / "config" / "cache")
        titles = {b["title"] for b in calibre.read_books(snap)}
        assert "Only In The WAL" in titles
    finally:
        c.close()


def test_snapshot_errors(env):
    with pytest.raises(calibre.CalibreError, match="No metadata.db"):
        calibre.snapshot(env / "nowhere", env / "config" / "cache")
    bad = env / "bad"
    bad.mkdir()
    (bad / "metadata.db").write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(calibre.CalibreError, match="consistent copy"):
        calibre.snapshot(bad, env / "config" / "cache", attempts=2, wait=0)


def test_read_books_fields(env):
    snap = calibre.snapshot(make_calibre(env / "calibre"), env / "config" / "cache")
    books = {b["calibre_id"]: b for b in calibre.read_books(snap)}
    b = books[1]
    assert b["isbn"] == "9780261102217" and b["formats"] == ",EPUB,MOBI," and b["series"] == "Middle-earth"
    assert b["pubdate"] is None  # Calibre's 0101-01-01 means unknown
    assert books[2]["ol_id"] == "OL27448W"
    assert books[4]["language"] == "eng"
    assert books[6]["formats"] is None


def test_read_books_newer_calibre_schema(env):
    """Newer Calibre versions have no books.isbn column (ISBNs live only in identifiers)."""
    d = make_calibre(env / "calibre")
    c = sqlite3.connect(d / "metadata.db")
    c.execute("ALTER TABLE books DROP COLUMN isbn")
    c.execute("ALTER TABLE books DROP COLUMN lccn")
    c.execute("ALTER TABLE books DROP COLUMN has_cover")
    c.commit()
    c.close()
    snap = calibre.snapshot(d, env / "config" / "cache")
    books = {b["calibre_id"]: b for b in calibre.read_books(snap)}
    assert books[1]["isbn"] == "9780261102217"   # from the identifiers table
    assert books[1]["has_cover"] == 0 and len(books) == 6


def test_read_books_without_optional_tables(env):
    d = make_calibre(env / "calibre")
    c = sqlite3.connect(d / "metadata.db")
    c.executescript("DROP TABLE identifiers; DROP TABLE books_languages_link; DROP TABLE books_series_link;")
    c.close()
    books = list(calibre.read_books(calibre.snapshot(d, env / "config" / "cache")))
    assert len(books) == 6 and all(b["isbn"] is None and b["series"] is None for b in books)


# --- sync -------------------------------------------------------------------------

def test_sync_matches_library(conn, lib, env):  # noqa: F811
    before = _hash_dir(lib)
    r = _sync(conn, env)
    assert r["done"] == 6 and r["removed"] == 0
    got = {cid: (_row(conn, cid)["work_key"], _row(conn, cid)["match_method"]) for cid in range(1, 7)}
    assert got == {
        1: ("OL262758W", "isbn"),
        2: ("OL27448W", "openlibrary"),
        3: ("OL1W", "title_author"),
        4: ("OL2W", "title_author"),
        5: (None, None),
        6: ("OL3W", "title_author"),
    }
    assert _hash_dir(lib) == before  # Calibre untouched
    assert _row(conn, 2)["authors"] == "J.R.R. Tolkien"   # "Tolkien, J.R.R." cleaned for display


def test_resync_removes_deleted_and_keeps_manual(conn, lib, env):  # noqa: F811
    _sync(conn, env)
    library.set_match(conn, 5, "OL2W")          # user says the grocery list is Hamlet (why not)
    assert _row(conn, 5)["match_method"] == "manual"
    c = sqlite3.connect(lib / "metadata.db")
    c.execute("DELETE FROM books WHERE id=6")
    c.execute("UPDATE books SET title='Hamlet', last_modified='2025-01-01' WHERE id=4")
    c.commit()
    c.close()
    r = _sync(conn, env, rematch=True)
    assert r["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM library_books").fetchone()[0] == 5
    assert _row(conn, 5)["work_key"] == "OL2W" and _row(conn, 5)["match_locked"] == 1
    assert _row(conn, 4)["title"] == "Hamlet"
    assert library.clear_manual(conn, 5)
    _sync(conn, env)
    assert _row(conn, 5)["work_key"] is None and _row(conn, 5)["match_locked"] == 0


def test_sync_flags_wrong_calibre_isbn(conn, lib, env):  # noqa: F811
    c = sqlite3.connect(lib / "metadata.db")
    c.execute("INSERT INTO books(id, title, author_sort) VALUES(7, 'Hamlet', 'Shakespeare| William')")
    c.execute("INSERT INTO authors(id, name, sort) VALUES(99, 'Shakespeare| William', 'Shakespeare| William')")
    c.execute("INSERT INTO books_authors_link(book, author) VALUES(7, 99)")
    c.execute("INSERT INTO identifiers(book, type, val) VALUES(7, 'isbn', '0261102214')")  # The Hobbit's ISBN
    c.commit()
    c.close()
    _sync(conn, env)
    r = _row(conn, 7)
    assert (r["work_key"], r["issues"], r["authors"]) == ("OL2W", "isbn_mismatch", "William Shakespeare")
    page = library.list_books(conn, filt="issues")
    assert page["total"] == 1 and page["items"][0]["issues"] == ["isbn_mismatch"]
    assert library.stats(conn)["issues"] == 1


def test_set_match_rejects_unknown_work(conn, lib, env):  # noqa: F811
    _sync(conn, env)
    with pytest.raises(ValueError):
        library.set_match(conn, 1, "OL404W")
    assert library.set_match(conn, 404, "OL2W") is None


def test_stats_and_listing(conn, lib, env):  # noqa: F811
    _sync(conn, env)
    s = library.stats(conn)
    assert s["books"] == 6 and s["matched"] == 5 and s["unmatched"] == 1
    assert s["not_epub"] == 4  # LOTR (PDF), Silmarillion (AZW3), grocery list (PDF), Macbeth (no files)
    assert s["by_method"] == {"isbn": 1, "openlibrary": 1, "title_author": 3}
    assert s["formats"]["EPUB"] == 2
    page = library.list_books(conn, filt="not_epub")
    assert page["total"] == 4
    assert library.list_books(conn, q="tolkien")["total"] == 3
    assert library.list_books(conn, q="hobbit")["items"][0]["work_title"] == "The Hobbit"
    assert library.list_books(conn, filt="unmatched")["items"][0]["calibre_id"] == 5
    assert library.list_books(conn, limit=2, offset=4)["items"][0]["calibre_id"] in range(1, 7)


def test_sync_without_catalog_just_mirrors(env):
    s = get_settings()
    c = db.connect(s.db_path)
    db.init(c)
    db.set_meta(c, "setting_calibre_path", str(make_calibre(env / "calibre")))
    r = _sync(c, env)
    assert r["done"] == 6 and r["matched"] == 0
    assert c.execute("SELECT COUNT(*) FROM library_books WHERE work_key IS NULL").fetchone()[0] == 6
    c.close()


# --- API ------------------------------------------------------------------------

def _wait(client, ids, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
        if all(jobs[i]["status"] not in ("queued", "running") for i in ids):
            return jobs
        time.sleep(0.1)
    raise AssertionError("jobs did not finish")


def test_api_library_flow(env, monkeypatch):
    from app.main import create_app
    lib_dir = make_calibre(env / "calibre")
    with TestClient(create_app()) as client:
        _wait(client, client.post("/api/catalog/import", json={}).json()["job_ids"])

        assert client.get("/api/settings").json()["calibre_path"] == "/calibre"
        bad = client.post("/api/library/test", json={}).json()
        assert bad["ok"] is False and "No metadata.db" in bad["message"]
        assert client.put("/api/settings", json={"calibre_path": "relative/path"}).status_code == 400
        assert client.put("/api/settings", json={"calibre_path": str(lib_dir) + "/"}).json()["calibre_path"] == str(lib_dir)
        ok = client.post("/api/library/test", json={}).json()
        assert ok["ok"] and ok["books"] == 6 and ok["formats"]["EPUB"] == 2

        job = client.post("/api/library/sync", json={}).json()["job_id"]
        jobs = _wait(client, [job])
        assert jobs[job]["status"] == "done", jobs[job]
        assert jobs[job]["progress"]["done"] == 6

        assert client.get("/api/library/stats").json()["matched"] == 5
        assert client.get("/api/library?filter=unmatched").json()["total"] == 1
        assert client.get("/api/library?filter=nope").status_code == 400

        hob = client.get("/api/search/works?q=hobbit").json()
        owned = {w["ol_key"]: w["owned"] for w in hob}
        assert owned["OL262758W"] == ["EPUB", "MOBI"] and owned["OL6W"] is None
        w = client.get("/api/works/OL3W").json()
        assert w["owned"] == [] and w["library_books"][0]["calibre_id"] == 6   # owned, but no files
        a = client.get("/api/authors/OL26320A").json()
        assert {x["ol_key"]: x["owned"] for x in a["works"]}["OL27448W"] == ["PDF"]

        assert client.put("/api/library/5/match", json={"work_key": "OL404W"}).status_code == 400
        assert client.put("/api/library/404/match", json={"work_key": "OL2W"}).status_code == 404
        assert client.put("/api/library/5/match", json={"work_key": "OL2W"}).json()["match_method"] == "manual"
        assert client.delete("/api/library/5/match").json() == {"ok": True}
        assert client.get("/api/status").json()["schema_version"] == db.SCHEMA_VERSION
