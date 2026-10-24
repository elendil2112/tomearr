"""Phase 4: ingest scanner. Identifies every file, matches it, buckets it. Never moves or changes anything."""
import hashlib
import os
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import db, extract, filenames, ingest, library
from app.config import get_settings
from conftest import make_calibre
from samples import (_pdf_bytes, make_docx, make_epub, make_fb2, make_gutenberg_txt, make_mobi, make_pdf,
                     make_zip)
from test_catalog import _no, _noop, conn  # noqa: F401


def _tree_hashes(root):
    out = {}
    for dirpath, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (hashlib.sha256(open(p, "rb").read()).hexdigest(), st.st_mtime_ns)
    return out


@pytest.fixture()
def drop(env, conn):  # noqa: F811
    """An ingest folder with one of everything."""
    d = env / "ingest"
    make_epub(d / "Tolkien/The Hobbit/The Hobbit - J.R.R. Tolkien.epub", isbn="0261102214")  # matched by ISBN
    make_epub(d / "dupes/copy of hobbit.epub", isbn="0261102214")                          # same bytes -> duplicate
    make_mobi(d / "lotr.azw3", title="The Lord of the Rings", author="J.R.R. Tolkien", kf8=True)
    make_pdf(d / "scans/random-scan-0042.pdf", ["ISBN 978-0-261-10221-7"])                   # ISBN only from text
    make_pdf(d / "Hamlet (William Shakespeare).pdf", ["page one"])                          # filename only
    make_pdf(d / "the silmarillion.pdf", ["chapter one"])                                   # title only -> review
    make_pdf(d / "Quarterly Tax Notes 2019.pdf", ["tax notes"])                             # nothing like it
    make_epub(d / "locked.epub", title="Macbeth", authors=["William Shakespeare"], drm="adept")
    make_fb2(d / "plays/hamlet.fb2")
    make_docx(d / "macbeth.docx")
    make_gutenberg_txt(d / "lotr-gutenberg.txt")
    make_zip(d / "bundle.zip", {"inner.epub": b"x"})
    make_zip(d / "comic.cbz", {"001.jpg": b"x", "002.jpg": b"y"})
    (d / "broken.epub").write_bytes(b"PK\x03\x04not really a zip")
    # things that must be skipped
    (d / "readme.txt").write_text("small notes")
    (d / "cover.jpg").write_bytes(b"\xff\xd8")
    (d / "downloading.epub.part").write_bytes(b"x")
    (d / ".hidden.epub").write_bytes(b"x")
    (d / "@eaDir").mkdir()
    make_epub(d / "@eaDir/thumb.epub")
    # metadata.opf sidecar names a book whose file says nothing useful
    (d / "sidecar").mkdir()
    (d / "sidecar/book.pdf").write_bytes(_pdf_bytes(["nothing here"]))
    (d / "sidecar/metadata.opf").write_text(
        '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Macbeth</dc:title><dc:creator>William Shakespeare</dc:creator></metadata></package>")
    old = time.time() - 3600
    for dirpath, _d, files in os.walk(d):
        for f in files:
            os.utime(os.path.join(dirpath, f), (old, old))
    db.set_meta(conn, "setting_ingest_path", str(d))
    return d


def _scan(conn, **kw):  # noqa: F811
    kw.setdefault("n_workers", 0)
    return ingest.scan(conn, get_settings(), _noop, _no, **kw)


def _f(conn, path):  # noqa: F811
    return conn.execute("SELECT * FROM ingest_files WHERE path=?", (path,)).fetchone()


# --- filename parsing ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,title,author", [
    ("J.R.R. Tolkien - The Hobbit.epub", "The Hobbit", "J.R.R. Tolkien"),
    ("The Hobbit - J.R.R. Tolkien (retail).epub", "The Hobbit", "J.R.R. Tolkien"),
    ("Brandon Sanderson - Mistborn 01 - The Final Empire.epub", "The Final Empire", "Brandon Sanderson"),
    ("The.Hobbit.by.J.R.R.Tolkien.mobi", "The Hobbit", "J R R Tolkien"),
    ("Dune (Frank Herbert).azw3", "Dune", "Frank Herbert"),
    ("Stephen King/Misery/book.epub", "book", "Stephen King"),
    ("Tolkien-The Silmarillion.epub", "The Silmarillion", "Tolkien"),
])
def test_filename_hypotheses_include_the_right_reading(name, title, author):
    hs = filenames.hypotheses(name)
    assert any(h["title"] == title and author in h["authors"] for h in hs), hs


def test_filename_cleaning():
    assert filenames.clean("the_hobbit_(z-lib.org)") == "the hobbit"
    assert filenames.clean("Dune [EPUB] (1965) (1)") == "Dune"


# --- extractors -----------------------------------------------------------------------------------------------

def test_extract_formats(tmp_path):
    cases = {
        "a.epub": (make_epub(tmp_path / "a.epub", isbn="0261102214", series=("Middle-earth", "1")), "EPUB", False),
        "b.epub": (make_epub(tmp_path / "b.epub", drm="adept"), "EPUB", True),
        "c.epub": (make_epub(tmp_path / "c.epub", drm="encrypted"), "EPUB", True),
        "d.epub": (make_epub(tmp_path / "d.epub", drm="fonts"), "EPUB", False),   # font obfuscation is not DRM
        "e.mobi": (make_mobi(tmp_path / "e.mobi", isbn="9780261102217"), "MOBI", False),
        "f.mobi": (make_mobi(tmp_path / "f.mobi", kf8=True), "AZW3", False),
        "g.azw": (make_mobi(tmp_path / "g.azw", encryption=2), "AZW", True),
        "h.pdf": (make_pdf(tmp_path / "h.pdf", ["ISBN 978-0-261-10221-7"]), "PDF", False),
        "i.pdf": (make_pdf(tmp_path / "i.pdf", ["x"], password="user"), "PDF", True),
        "j.pdf": (make_pdf(tmp_path / "j.pdf", ["ISBN 0261102214"], password="owner"), "PDF", False),
        "k.fb2": (make_fb2(tmp_path / "k.fb2", isbn="0261102214"), "FB2", False),
        "l.fb2.zip": (make_fb2(tmp_path / "l.fb2.zip", zipped=True), "FB2", False),
        "m.docx": (make_docx(tmp_path / "m.docx"), "DOCX", False),
        "n.txt": (make_gutenberg_txt(tmp_path / "n.txt"), "TXT", False),
    }
    for name, (path, fmt, drm) in cases.items():
        r = extract.analyze(path)
        assert (r["format"], r["drm"], r["error"]) == (fmt, drm, None), (name, r)
        assert r["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    a = extract.analyze(tmp_path / "a.epub")["meta"]
    assert (a["title"], a["authors"], a["isbns"], a["series"], a["series_index"], a["year"]) == \
        ("The Hobbit", ["J.R.R. Tolkien"], ["9780261102217"], "Middle-earth", 1.0, 1937)
    assert extract.analyze(tmp_path / "h.pdf")["meta"]["isbns"] == ["9780261102217"]
    assert extract.analyze(tmp_path / "j.pdf")["meta"]["isbns"] == ["9780261102217"]
    assert extract.analyze(tmp_path / "n.txt")["meta"]["title"] == "The Lord of the Rings"


def test_extract_odd_files(tmp_path):
    (tmp_path / "x.kfx").write_bytes(b"\xeaDRMION\xee" + b"0" * 100)
    assert extract.analyze(tmp_path / "x.kfx")["drm"] is True
    (tmp_path / "really-a-pdf.epub").write_bytes(_pdf_bytes(["hi"]))
    assert extract.analyze(tmp_path / "really-a-pdf.epub")["format"] == "PDF"   # contents beat the extension
    (tmp_path / "bad.epub").write_bytes(b"PK\x03\x04junk")
    r = extract.analyze(tmp_path / "bad.epub")
    assert r["error"] and r["format"] == "EPUB" and r["sha256"]
    make_zip(tmp_path / "b.zip", {"a/inner.mobi": b"x", "notes.nfo": b"y"})
    r = extract.analyze(tmp_path / "b.zip")
    assert r["kind"] == "archive" and r["entries"] == ["a/inner.mobi"]
    # junk embedded metadata is ignored rather than trusted
    make_pdf(tmp_path / "w.pdf", title="Microsoft Word - draft3.docx", author="Administrator")
    m = extract.analyze(tmp_path / "w.pdf")["meta"]
    assert m is None or (m["title"] is None and m["authors"] == [])


# --- scanning -------------------------------------------------------------------------------------------------

def test_scan_buckets_everything_and_moves_nothing(conn, drop):  # noqa: F811
    before = _tree_hashes(drop)
    r = _scan(conn)
    assert _tree_hashes(drop) == before                      # report only: nothing moved, nothing modified
    assert r["partial"] == 1 and r["small_txt"] == 1 and r["ignored"] >= 1
    got = {row["path"]: row["bucket"] for row in conn.execute("SELECT path, bucket FROM ingest_files")}
    assert got == {
        "Tolkien/The Hobbit/The Hobbit - J.R.R. Tolkien.epub": "matched",
        "dupes/copy of hobbit.epub": "duplicate",
        "lotr.azw3": "matched",
        "scans/random-scan-0042.pdf": "matched",
        "Hamlet (William Shakespeare).pdf": "matched",
        "the silmarillion.pdf": "review",
        "Quarterly Tax Notes 2019.pdf": "unmatched",
        "locked.epub": "drm",
        "plays/hamlet.fb2": "matched",
        "macbeth.docx": "matched",
        "lotr-gutenberg.txt": "matched",
        "bundle.zip": "archive",
        "comic.cbz": "comic",
        "broken.epub": "error",
        "sidecar/book.pdf": "matched",
    }
    hob = _f(conn, "Tolkien/The Hobbit/The Hobbit - J.R.R. Tolkien.epub")
    assert (hob["work_key"], hob["match_method"], hob["match_source"]) == ("OL262758W", "isbn", "embedded")
    assert _f(conn, "dupes/copy of hobbit.epub")["duplicate_of"] == "ingest:Tolkien/The Hobbit/The Hobbit - J.R.R. Tolkien.epub"
    assert _f(conn, "scans/random-scan-0042.pdf")["work_key"] == "OL262758W"
    assert _f(conn, "Hamlet (William Shakespeare).pdf")["match_source"].startswith("filename:")
    assert _f(conn, "sidecar/book.pdf")["match_source"] == "sidecar"
    assert _f(conn, "sidecar/book.pdf")["work_key"] == "OL3W"
    assert _f(conn, "locked.epub")["work_key"] == "OL3W"         # DRM files are still identified
    assert _f(conn, "lotr.azw3")["format"] == "AZW3"
    assert "BadZipFile" in _f(conn, "broken.epub")["error"]


def test_incremental_rescan(conn, drop):  # noqa: F811
    _scan(conn)
    r = _scan(conn)
    assert r["read"] == 0 and r["removed"] == 0                # nothing changed, nothing re-read
    os.utime(drop / "lotr.azw3", (time.time() - 1000, time.time() - 1000))
    (drop / "macbeth.docx").unlink()
    r = _scan(conn)
    assert (r["read"], r["removed"]) == (1, 1)
    assert _f(conn, "macbeth.docx") is None
    assert _scan(conn, full=True)["read"] == 14


def test_files_still_arriving_are_left_alone(conn, drop):  # noqa: F811
    make_epub(drop / "just-landed.epub", title="Hamlet", authors=["William Shakespeare"])  # mtime = now
    r = _scan(conn)
    assert r["settling"] == 1 and _f(conn, "just-landed.epub") is None
    db.set_meta(conn, "setting_ingest_settle_sec", 0)
    _scan(conn)
    assert _f(conn, "just-landed.epub")["bucket"] == "matched"


def test_duplicate_of_a_calibre_file(conn, drop, env):  # noqa: F811
    lib = make_calibre(env / "calibre")
    src = drop / "lotr.azw3"
    (lib / "Tolkien/LOTR (2)").mkdir(parents=True)
    shutil.copy(src, lib / "Tolkien/LOTR (2)/LOTR - Tolkien.azw3")
    c = sqlite3.connect(lib / "metadata.db")
    c.execute("UPDATE books SET path='Tolkien/LOTR (2)' WHERE id=2")
    c.execute("INSERT INTO data(book, format, uncompressed_size, name) VALUES(2, 'AZW3', ?, 'LOTR - Tolkien')",
              (src.stat().st_size,))
    c.commit()
    c.close()
    db.set_meta(conn, "setting_calibre_path", str(lib))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    before = _tree_hashes(lib)
    _scan(conn)
    row = _f(conn, "lotr.azw3")
    assert (row["bucket"], row["duplicate_of"]) == ("duplicate", "calibre:2")
    assert _tree_hashes(lib) == before
    # matched books you already own say which formats Calibre has
    assert _f(conn, "Tolkien/The Hobbit/The Hobbit - J.R.R. Tolkien.epub")["owned_formats"] == "EPUB,MOBI"
    # only same-size Calibre files were hashed
    hashed = conn.execute("SELECT COUNT(*) FROM calibre_files WHERE sha256 IS NOT NULL").fetchone()[0]
    assert hashed == 1


def test_rematch_after_catalog_change(conn, drop):  # noqa: F811
    _scan(conn)
    conn.execute("UPDATE ingest_files SET work_key=NULL, match_confidence=0, bucket='unmatched'")
    conn.commit()
    r = _scan(conn, rematch=True)
    assert r["read"] == 0 and r["rematched"] == 15
    assert _f(conn, "lotr.azw3")["bucket"] == "matched"


def test_worker_pool_and_timeouts(conn, drop, monkeypatch):  # noqa: F811
    r = _scan(conn, n_workers=2)                      # real worker processes
    assert r["read"] == 15
    assert _f(conn, "lotr.azw3")["bucket"] == "matched"
    # A file that never finishes reading (a FIFO with no writer) times out and the scan carries on.
    os.mkfifo(drop / "stuck.epub")
    monkeypatch.setattr(ingest, "_timeout_for", lambda size: 2)
    rels = ["lotr.azw3", "stuck.epub", "macbeth.docx", "plays/hamlet.fb2"]
    out = dict(ingest.read_files(drop, rels, {}, _no, n_workers=1))
    assert out["stuck.epub"]["error"] == "timed out reading this file"
    assert out["macbeth.docx"]["format"] == "DOCX" and out["plays/hamlet.fb2"]["format"] == "FB2"


def test_missing_folder(conn, env):  # noqa: F811
    db.set_meta(conn, "setting_ingest_path", str(env / "nope"))
    with pytest.raises(FileNotFoundError, match="mounted"):
        _scan(conn)


# --- scheduler ------------------------------------------------------------------------------------------------

def test_scheduler(env, conn, drop):  # noqa: F811
    from app.scheduler import Scheduler

    class FakeRunner:
        def __init__(self):
            self.kinds = []

        def submit(self, kind, params=None, depends_on=None):
            self.kinds.append(kind)
            conn.execute("INSERT INTO jobs(kind, params, status) VALUES(?, '{}', 'queued')", (kind,))
            conn.commit()

    runner = FakeRunner()
    s = Scheduler(get_settings(), runner)
    assert s.tick() == ["ingest_scan"]                 # due; Calibre isn't set up, so no library sync
    assert s.tick() == []                              # already queued
    conn.execute("UPDATE jobs SET status='done', created_at=datetime('now')")
    conn.commit()
    db.set_meta(conn, "ingest_scanned_at", conn.execute("SELECT datetime('now')").fetchone()[0])
    assert s.tick() == []                              # ran just now; next one in 5 minutes
    conn.execute("UPDATE jobs SET created_at=datetime('now', '-10 minutes')")
    conn.commit()
    db.set_meta(conn, "ingest_scanned_at", conn.execute("SELECT datetime('now', '-10 minutes')").fetchone()[0])
    assert s.tick() == ["ingest_scan"]
    db.set_meta(conn, "setting_ingest_interval_min", 0)  # 0 turns it off
    conn.execute("UPDATE jobs SET status='done'")
    conn.commit()
    assert s.tick() == []


# --- API --------------------------------------------------------------------------------------------------------

def _wait(client, ids, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
        if all(jobs[i]["status"] not in ("queued", "running") for i in ids):
            return jobs
        time.sleep(0.1)
    raise AssertionError("jobs did not finish")


def test_api_ingest_flow(env, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("TOMEARR_WORKERS", "0")
    d = env / "ingest"
    make_epub(d / "a.epub", isbn="0261102214")
    make_pdf(d / "Quarterly Tax Notes.pdf")
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))
    with TestClient(create_app()) as client:
        _wait(client, client.post("/api/catalog/import", json={}).json()["job_ids"])
        s = client.get("/api/settings").json()
        assert (s["ingest_path"], s["ingest_interval_min"], s["ingest_settle_sec"]) == ("/ingest", 5, 120)
        assert client.post("/api/ingest/test", json={}).json()["ok"] is False
        assert client.put("/api/settings", json={"ingest_path": "rel"}).status_code == 400
        assert client.put("/api/settings", json={"ingest_interval_min": -1}).status_code == 400
        s = client.put("/api/settings", json={"ingest_path": str(d), "ingest_interval_min": 0}).json()
        assert s["ingest_path"] == str(d) and s["ingest_interval_min"] == 0
        t = client.post("/api/ingest/test", json={}).json()
        assert t["ok"] and t["files"] == 2 and t["by_kind"] == {"ebook": 2}

        job = client.post("/api/ingest/scan", json={}).json()["job_id"]
        jobs = _wait(client, [job])
        assert jobs[job]["status"] == "done", jobs[job]
        st = client.get("/api/ingest/stats").json()
        assert st["files"] == 2 and st["buckets"]["matched"] == 1 and st["buckets"]["unmatched"] == 1
        assert st["formats"] == {"EPUB": 1, "PDF": 1} and st["last_scan"]["read"] == 2
        items = client.get("/api/ingest?bucket=matched").json()["items"]
        assert items[0]["work_title"] == "The Hobbit" and items[0]["meta"]["isbns"] == ["9780261102217"]
        assert client.get("/api/ingest?q=quarterly").json()["total"] == 1
        un = client.get("/api/ingest?bucket=unmatched").json()["items"][0]
        assert isinstance(un["candidates"], list)
        assert client.get("/api/ingest?bucket=nope").status_code == 400
        assert client.get("/api/status").json()["schema_version"] == db.SCHEMA_VERSION
