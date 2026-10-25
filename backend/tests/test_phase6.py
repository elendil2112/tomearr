"""Phase 6: import into Calibre. These run against a real calibre-server (skipped if Calibre isn't installed)."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import calibre_cli, db, importer, ingest, library, review
from app.config import get_settings
from samples import make_docx, make_epub, make_fb2, make_mobi, make_pdf, make_zip
from test_catalog import _no, _noop, conn  # noqa: F401

pytestmark = pytest.mark.skipif(calibre_cli.tool("calibredb") is None or shutil.which("calibre-server") is None,
                                reason="Calibre is not installed")
PASSWORD = "s3cret pass"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _cdb(lib, *args):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    out = subprocess.run(["calibredb", *args, "--library-path", str(lib)], capture_output=True, text=True,
                         env=env, check=True).stdout
    return out


def _books(lib):
    """Read Calibre's metadata.db directly (read-only). calibredb itself refuses while a server is running."""
    import sqlite3
    c = sqlite3.connect(f"file:{lib / 'metadata.db'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out = {}
    for b in c.execute("SELECT id, title, series_index FROM books"):
        bid = b["id"]
        authors = " & ".join(r[0] for r in c.execute(
            "SELECT a.name FROM books_authors_link l JOIN authors a ON a.id = l.author WHERE l.book=? ORDER BY l.id", (bid,)))
        series = c.execute("SELECT s.name FROM books_series_link l JOIN series s ON s.id = l.series WHERE l.book=?",
                           (bid,)).fetchone()
        out[bid] = {
            "id": bid, "title": b["title"], "authors": authors,
            "formats": sorted(r[0] for r in c.execute("SELECT format FROM data WHERE book=?", (bid,))),
            "identifiers": {r[0]: r[1] for r in c.execute("SELECT type, val FROM identifiers WHERE book=?", (bid,))},
            "series": series[0] if series else None, "series_index": b["series_index"] if series else None,
        }
    c.close()
    return out


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture(params=["direct", "server"])
def calibre(request, env, conn, monkeypatch):  # noqa: F811
    """A real Calibre library. 'direct': Tomearr writes it with calibredb --library-path (the default, no login).
    'server': through calibre-server with a Calibre user."""
    cfg = env / "calibre-config"
    cfg.mkdir()
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(cfg))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    lib = env / "Calibre Library"
    lib.mkdir()
    src = env / "seed"
    # already in Calibre: Hamlet as MOBI only, and Macbeth as AZW3 only
    make_mobi(src / "hamlet.mobi", title="Hamlet", author="William Shakespeare")
    make_mobi(src / "macbeth.azw3", title="Macbeth", author="William Shakespeare", kf8=True)
    _cdb(lib, "add", "--title", "Hamlet", "--authors", "William Shakespeare", str(src / "hamlet.mobi"))
    _cdb(lib, "add", "--title", "Macbeth", "--authors", "William Shakespeare", str(src / "macbeth.azw3"))
    if request.param == "direct":
        for k, v in {"calibre_mode": "direct", "calibre_path": str(lib)}.items():
            db.set_meta(conn, f"setting_{k}", v)
        library.sync(conn, env / "config" / "cache", _noop, _no)
        yield lib
        return
    users = cfg / "users.sqlite"
    subprocess.run(["calibre-server", "--userdb", str(users), "--manage-users", "--", "add", "tomearr", PASSWORD],
                   check=True, capture_output=True, env={**os.environ})
    port = _free_port()
    proc = subprocess.Popen(["calibre-server", "--port", str(port), "--listen-on", "127.0.0.1", "--enable-auth",
                             "--userdb", str(users), "--disable-use-bonjour", str(lib)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ})
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.3)
    for k, v in {"calibre_mode": "server", "calibre_url": f"http://127.0.0.1:{port}", "calibre_username": "tomearr",
                 "calibre_password": PASSWORD, "calibre_path": str(lib)}.items():
        db.set_meta(conn, f"setting_{k}", v)
    library.sync(conn, env / "config" / "cache", _noop, _no)
    yield lib
    proc.terminate()
    proc.wait(10)


@pytest.fixture()
def drop(env, conn):  # noqa: F811
    d = env / "ingest"
    make_epub(d / "The Hobbit - J.R.R. Tolkien.epub", isbn="0261102214", series=("Middle-earth", "1"))
    make_mobi(d / "lotr.mobi", title="The Lord of the Rings", author="J.R.R. Tolkien")
    make_fb2(d / "plays/hamlet.fb2")                                   # Calibre has Hamlet as MOBI
    make_docx(d / "silmarillion.docx", title="The Silmarillion", creator="J.R.R. Tolkien")
    make_zip(d / "bundle.zip", {"inner/The Hobbit Companion.epub": make_epub(
        d.parent / "tmp/companion.epub", title="The Hobbit Companion", authors=["Hobbit Fan"]).read_bytes()})
    make_pdf(d / "Hamlet (William Shakespeare).pdf", ["Act one"])
    make_epub(d / "locked.epub", title="Macbeth", authors=["William Shakespeare"], drm="adept")
    old = time.time() - 3600
    for dp, _d, fs in os.walk(d):
        for f in fs:
            os.utime(os.path.join(dp, f), (old, old))
    db.set_meta(conn, "setting_ingest_path", str(d))
    return d


def _scan(conn):  # noqa: F811
    return ingest.scan(conn, get_settings(), _noop, _no, n_workers=0)


def _run(conn, **kw):  # noqa: F811
    return importer.run(conn, get_settings(), _noop, _no, batch=kw.pop("batch", 1), **kw)


def test_connection_and_errors(conn, calibre):  # noqa: F811
    t = calibre_cli.Calibre(importer.target(conn)).test()
    assert t["ok"] and t["books"] == 2 and t["version"]
    if importer.target(conn).mode == "direct":
        os.chmod(calibre / "metadata.db", 0o444)
        try:
            if not os.access(calibre / "metadata.db", os.W_OK):
                with pytest.raises(calibre_cli.CalibreCLIError, match="read-only"):
                    calibre_cli.Calibre(importer.target(conn)).test()
        finally:
            os.chmod(calibre / "metadata.db", 0o644)
        db.set_meta(conn, "setting_calibre_path", str(calibre.parent / "nope"))
        with pytest.raises(calibre_cli.CalibreCLIError, match="No metadata.db"):
            calibre_cli.Calibre(importer.target(conn)).test()
        return
    db.set_meta(conn, "setting_calibre_password", "wrong")
    with pytest.raises(calibre_cli.CalibreCLIError, match="incorrect"):
        calibre_cli.Calibre(importer.target(conn)).test()
    db.set_meta(conn, "setting_calibre_url", "")
    with pytest.raises(calibre_cli.CalibreCLIError, match="server address"):
        calibre_cli.Calibre(importer.target(conn)).test()


def test_plan_is_a_dry_run(conn, calibre, drop):  # noqa: F811
    _scan(conn)
    before_files = {p: _sha(drop / p) for p in ("lotr.mobi", "plays/hamlet.fb2")}
    before_books = _books(calibre)
    p = importer.plan(conn)
    kinds = {i["path"]: i["kind"] for i in p["items"]}
    assert kinds == {
        "The Hobbit - J.R.R. Tolkien.epub": "add",
        "lotr.mobi": "add",
        "plays/hamlet.fb2": "add_format",
        "silmarillion.docx": "add",
        "Hamlet (William Shakespeare).pdf": "pdf_hold",
        "bundle.zip": "unpack",
    }
    ham = next(i for i in p["items"] if i["path"] == "plays/hamlet.fb2")
    assert ham["remove"] == ["MOBI"] and any("remove MOBI" in s for s in ham["steps"])
    assert p["selected"] == 5
    assert _books(calibre) == before_books
    assert {k: _sha(drop / k) for k in before_files} == before_files


def test_import_undo_round_trip(conn, calibre, drop, env):  # noqa: F811
    _scan(conn)
    lib_before = _books(calibre)
    ingest_before = {str(p.relative_to(drop)): _sha(p) for p in drop.rglob("*") if p.is_file()}
    r = _run(conn, batch=7)
    assert (r["done"], r["failed"]) == (5, 0), [(h["path"], h["error"]) for h in importer.history(conn) if h["error"]]

    books = _books(calibre)
    by_title = {b["title"]: b for b in books.values()}
    assert len(books) == 5                                         # 2 existing + hobbit, lotr, silmarillion
    hob = by_title["The Hobbit"]
    assert hob["formats"] == ["EPUB"] and hob["authors"] == "J.R.R. Tolkien"
    assert hob["identifiers"]["openlibrary"] == "OL262758W" and hob["identifiers"]["isbn"] == "9780261102217"
    assert hob["series"] == "Middle-earth" and hob["series_index"] == 1.0
    assert by_title["The Lord of the Rings"]["formats"] == ["EPUB"]                 # converted from MOBI
    assert by_title["The Silmarillion"]["formats"] == ["EPUB"]                      # converted from DOCX
    assert by_title["Hamlet"]["formats"] == ["EPUB"]                                # EPUB added, MOBI removed
    assert by_title["Macbeth"]["formats"] == ["AZW3"]                               # untouched
    # Calibre itself named the folders and files
    assert (calibre / "J.R.R. Tolkien").is_dir()
    assert list((calibre / "J.R.R. Tolkien").glob("The Hobbit (*)/The Hobbit - J.R.R. Tolkien.epub"))

    # originals left the ingest folder for the trash; the archive was unpacked in place
    left = {str(p.relative_to(drop)) for p in drop.rglob("*") if p.is_file()}
    assert left == {"Hamlet (William Shakespeare).pdf", "locked.epub",
                    "bundle (unpacked)/inner/The Hobbit Companion.epub"}
    trash = list((env / "config" / "trash").glob("*-job7"))
    assert len(trash) == 1 and (trash[0] / "ingest" / "lotr.mobi").is_file()
    assert list((trash[0] / "calibre").rglob("*.mobi"))                              # Hamlet's MOBI, backed up
    hist = importer.history(conn)
    assert {h["action"] for h in hist} == {"add", "add_format", "unpack"} and all(h["status"] == "done" for h in hist)
    # Tomearr's own view follows (the job runner does this after an import)
    library.sync(conn, env / "config" / "cache", _noop, _no)
    _scan(conn)
    assert conn.execute("SELECT COUNT(*) FROM library_books").fetchone()[0] == 5
    assert ingest.stats(conn)["files"] == 3

    # undo the whole batch: Calibre and the ingest folder are back where they were
    u = importer.undo_batch(conn, 7)
    assert u == {"undone": 5, "errors": []}
    after = _books(calibre)
    assert {i: (b["title"], b["formats"]) for i, b in after.items()} == \
        {i: (b["title"], b["formats"]) for i, b in lib_before.items()}
    restored = {str(p.relative_to(drop)): _sha(p) for p in drop.rglob("*") if p.is_file()}
    assert restored == ingest_before
    assert all(h["status"] == "undone" for h in importer.history(conn))
    with pytest.raises(importer.ImportError_):
        importer.undo(conn, hist[0]["id"])


def test_pdf_needs_opt_in_and_approval(conn, calibre, drop):  # noqa: F811
    _scan(conn)
    fid = conn.execute("SELECT id FROM ingest_files WHERE format='PDF'").fetchone()[0]
    with pytest.raises(importer.ImportError_, match="convert it first"):
        importer.pdf_action(conn, get_settings(), fid, "approve")
    importer.pdf_action(conn, get_settings(), fid, "convert")
    key = f"file:{fid}"
    assert next(i for i in importer.plan(conn)["items"] if i["key"] == key)["kind"] == "pdf_convert"
    r = _run(conn, keys=[key])
    assert r["done"] == 1
    assert (drop / "Hamlet (William Shakespeare).pdf").is_file()               # still there, nothing imported
    prev = importer.pdf_preview(get_settings(), fid)
    assert "Act one" in prev["sample"] and prev["size"] > 0
    assert "@page" not in prev["sample"]
    # undoing the conversion sends the PDF back to "held"
    conv = next(h for h in importer.history(conn) if h["action"] == "pdf_convert")
    importer.undo(conn, conv["id"], get_settings())
    assert next(i for i in importer.plan(conn)["items"] if i["key"] == key)["kind"] == "pdf_hold"
    importer.pdf_action(conn, get_settings(), fid, "convert")
    _run(conn, keys=[key])
    importer.pdf_action(conn, get_settings(), fid, "approve")
    it = next(i for i in importer.plan(conn)["items"] if i["key"] == key)
    assert it["kind"] == "add_format"                                            # Hamlet is in Calibre (MOBI)
    r = _run(conn, keys=[key])
    assert r["done"] == 1, importer.history(conn)
    assert "EPUB" in next(b for b in _books(calibre).values() if b["title"] == "Hamlet")["formats"]
    assert not (drop / "Hamlet (William Shakespeare).pdf").exists()


def test_convert_existing_library_books(conn, calibre, env):  # noqa: F811
    p = importer.plan_library(conn)
    assert {(i["title"], i["format"], tuple(i["remove"])) for i in p["items"]} == \
        {("Hamlet", "MOBI", ("MOBI",)), ("Macbeth", "AZW3", ("AZW3",))}
    before = _books(calibre)
    r = _run(conn, library=True, batch=3)
    assert (r["done"], r["failed"]) == (2, 0), importer.history(conn)
    assert {b["title"]: b["formats"] for b in _books(calibre).values()} == {"Hamlet": ["EPUB"], "Macbeth": ["EPUB"]}
    assert len(list((env / "config" / "trash").rglob("*.azw3"))) == 1
    importer.undo_batch(conn, 3)
    assert {i: b["formats"] for i, b in _books(calibre).items()} == {i: b["formats"] for i, b in before.items()}


def test_failures_leave_everything_in_place(conn, calibre, drop, env):  # noqa: F811
    _scan(conn)
    before = _books(calibre)
    # a file that changed after the scan is not imported
    (drop / "lotr.mobi").write_bytes((drop / "lotr.mobi").read_bytes() + b"changed")
    # a broken connection fails the whole run before anything happens
    good = db.get_meta(conn, "setting_calibre_path")
    db.set_meta(conn, "setting_calibre_path", good + "-missing")
    if importer.target(conn).mode == "server":
        db.set_meta(conn, "setting_calibre_password", "nope")
    with pytest.raises(calibre_cli.CalibreCLIError):
        _run(conn)
    db.set_meta(conn, "setting_calibre_path", good)
    db.set_meta(conn, "setting_calibre_password", PASSWORD)
    r = _run(conn)
    assert r["failed"] == 1
    failed = [h for h in importer.history(conn) if h["status"] == "failed"]
    assert failed[0]["path"] == "lotr.mobi" and "changed since it was scanned" in failed[0]["error"]
    assert (drop / "lotr.mobi").is_file()
    assert len(_books(calibre)) == len(before) + 2                              # hobbit + silmarillion only


def test_read_only_ingest_is_refused(conn, calibre, drop):  # noqa: F811
    _scan(conn)
    os.chmod(drop, 0o555)
    try:
        if os.access(drop, os.W_OK):
            pytest.skip("running as root: permissions are not enforced")
        with pytest.raises(importer.ImportError_, match="read-only"):
            _run(conn)
    finally:
        os.chmod(drop, 0o755)


def test_same_book_twice_in_one_run(conn, calibre, env):  # noqa: F811
    d = env / "twice"
    make_epub(d / "a.epub", title="The Lord of the Rings", authors=["J.R.R. Tolkien"])
    make_mobi(d / "b.mobi", title="The Lord of the Rings", author="J.R.R. Tolkien")
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))
    db.set_meta(conn, "setting_ingest_path", str(d))
    _scan(conn)
    r = _run(conn)
    assert (r["done"], r["failed"]) == (2, 0)
    lotr = [b for b in _books(calibre).values() if b["title"] == "The Lord of the Rings"]
    assert len(lotr) == 1 and lotr[0]["formats"] == ["EPUB"]
    assert sorted(h["action"] for h in importer.history(conn)) == ["add", "already_have"]


def _add_catalog_works(conn, n):  # noqa: F811
    """n distinct works by n/4 authors, so 100 files are 100 different books."""
    from app import ol_import
    for i in range(n // 4 + 1):
        conn.execute("INSERT INTO authors(ol_key, name) VALUES(?, ?)", (f"OL{900 + i}A", f"Testauthor Number{i:02d}"))
    words = ["Amber", "Basalt", "Cedar", "Delta", "Ember", "Fjord", "Garnet", "Harbor", "Indigo", "Juniper"]
    for i in range(n):
        title = f"The {words[i % 10]} {words[(i // 10) % 10]} Chronicle"
        conn.execute("INSERT INTO works(ol_key, title) VALUES(?, ?)", (f"OL{9000 + i}W", title))
        conn.execute("INSERT INTO work_authors VALUES(?, ?)", (f"OL{9000 + i}W", f"OL{900 + i // 4}A"))
    conn.commit()
    ol_import.build_indexes(conn, _noop)


def test_hundred_mixed_files(conn, calibre, env):  # noqa: F811
    """The plan's acceptance test: 100 mixed-format files land in Calibre as EPUBs with correct metadata, and
    undo restores the previous state."""
    _add_catalog_works(conn, 100)
    d = env / "ingest100"
    expected = {}
    for i in range(100):
        wk = f"OL{9000 + i}W"
        t = conn.execute("SELECT title FROM works WHERE ol_key=?", (wk,)).fetchone()[0]
        a = f"Testauthor Number{i // 4:02d}"
        name = f"{a} - {t}"
        k = i % 4
        if k == 0:
            p = make_epub(d / f"{name}.epub", title=t, authors=[a])
        elif k == 1:
            p = make_mobi(d / f"{name}.mobi", title=t, author=a)
        elif k == 2:
            p = make_mobi(d / f"{name}.azw3", title=t, author=a, kf8=True)
        else:
            p = make_fb2(d / f"{name}.fb2", title=t, first="Testauthor", last=f"Number{i // 4:02d}")
        expected[p.name] = (wk, t, a)
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))
    db.set_meta(conn, "setting_ingest_path", str(d))
    _scan(conn)
    assert ingest.stats(conn)["buckets"]["matched"] == 100
    before = _books(calibre)
    p = importer.plan(conn)
    assert p["counts"] == {"add": 100}
    t0 = time.time()
    r = _run(conn, batch=100)
    took = time.time() - t0
    assert (r["done"], r["failed"]) == (100, 0), [h["error"] for h in importer.history(conn) if h["error"]][:3]
    books = {i: b for i, b in _books(calibre).items() if i not in before}
    assert len(books) == 100 and all(b["formats"] == ["EPUB"] for b in books.values())
    got = {(b["title"], b["authors"], b["identifiers"].get("openlibrary")) for b in books.values()}
    assert got == {(t, a, wk) for wk, t, a in expected.values()}
    assert not list(d.iterdir())
    print(f"\n100 files imported in {took:.0f}s")
    u = importer.undo_batch(conn, 100)
    assert u["undone"] == 100 and not u["errors"]
    assert set(_books(calibre)) == set(before)
    assert {f.name for f in d.iterdir()} == set(expected)


def test_api_import_flow(env, monkeypatch):
    """Through the HTTP API and the job runner, direct mode, the way the UI drives it."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    monkeypatch.setenv("TOMEARR_WORKERS", "0")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(env / "calibre-config"))
    lib = env / "Calibre Library"
    lib.mkdir()
    make_mobi(env / "seed/hamlet.mobi", title="Hamlet", author="William Shakespeare")
    _cdb(lib, "add", "--title", "Hamlet", "--authors", "William Shakespeare", str(env / "seed/hamlet.mobi"))
    d = env / "ingest"
    make_epub(d / "hobbit.epub", isbn="0261102214")
    make_mobi(d / "lotr.azw3", title="The Lord of the Rings", author="J.R.R. Tolkien", kf8=True)
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))

    def wait(client, ids):
        deadline = time.time() + 120
        while time.time() < deadline:
            jobs = {j["id"]: j for j in client.get("/api/jobs?limit=50").json()}
            if all(i in jobs and jobs[i]["status"] not in ("queued", "running") for i in ids):
                return jobs
            time.sleep(0.2)
        raise AssertionError("jobs did not finish")

    with TestClient(create_app()) as client:
        wait(client, client.post("/api/catalog/import", json={}).json()["job_ids"])
        s = client.put("/api/settings", json={"ingest_path": str(d), "calibre_path": str(lib), "ingest_interval_min": 0,
                                              "library_sync_interval_min": 0, "calibre_password": "x"}).json()
        assert s["calibre_mode"] == "direct" and s["calibre_password_set"] is True and "calibre_password" not in s
        assert s["replace_formats"] == "AZW,AZW3,MOBI,PRC" or s["replace_formats"] == "MOBI,AZW,AZW3,PRC"
        assert client.put("/api/settings", json={"calibre_mode": "ftp"}).status_code == 400
        assert client.put("/api/settings", json={"calibre_url": "not a url"}).status_code == 400
        assert client.post("/api/calibre/test").json()["ok"] is True
        wait(client, [client.post("/api/library/sync", json={}).json()["job_id"]])
        wait(client, [client.post("/api/ingest/scan", json={}).json()["job_id"]])

        p = client.get("/api/import/plan").json()
        assert p["counts"] == {"add": 2} and p["selected"] == 2
        lp = client.get("/api/import/plan?library=true").json()
        assert lp["counts"] == {"convert_existing": 1}

        job = client.post("/api/import/run", json={}).json()["job_id"]
        jobs = wait(client, [job])
        assert jobs[job]["status"] == "done" and jobs[job]["progress"]["done"] == 2, jobs[job]
        # the follow-up sync and scan were queued by the import job
        follow = [j for j in client.get("/api/jobs?limit=50").json() if j["id"] > job]
        assert {j["kind"] for j in follow} == {"library_sync", "ingest_scan"}
        wait(client, [j["id"] for j in follow])
        assert client.get("/api/library/stats").json()["books"] == 3
        assert client.get("/api/ingest/stats").json()["files"] == 0

        hist = client.get("/api/import/history").json()
        assert len(hist) == 2 and {h["status"] for h in hist} == {"done"}
        r = client.post(f"/api/import/batch/{job}/undo").json()
        assert r == {"undone": 2, "errors": []}
        assert sorted(f.name for f in d.iterdir()) == ["hobbit.epub", "lotr.azw3"]
        assert client.post(f"/api/import/{hist[0]['id']}/undo").status_code == 400
        assert client.post("/api/import/99999/undo").status_code == 404
        assert client.get("/api/import/pdf/1/preview").status_code == 404


def test_trash_zero_deletes_originals_immediately(conn, calibre, drop, env):  # noqa: F811
    db.set_meta(conn, "setting_trash_days", "0")
    _scan(conn)
    r = _run(conn, batch=11)
    assert (r["done"], r["failed"]) == (5, 0), [h["error"] for h in importer.history(conn) if h["error"]]
    # originals gone straight away, nothing kept in a trash folder
    left = {str(p.relative_to(drop)) for p in drop.rglob("*") if p.is_file()}
    assert left == {"Hamlet (William Shakespeare).pdf", "locked.epub",
                    "bundle (unpacked)/inner/The Hobbit Companion.epub"}
    assert not (env / "config" / "trash").exists() or not any((env / "config" / "trash").rglob("*"))
    # the books are in Calibre; Hamlet's MOBI was replaced without a backup
    by_title = {b["title"]: b for b in _books(calibre).values()}
    assert by_title["Hamlet"]["formats"] == ["EPUB"] and by_title["The Lord of the Rings"]["formats"] == ["EPUB"]
    hist = importer.history(conn)
    assert all(h["meta"] and h["meta"].get("no_undo") for h in hist)
    assert next(h for h in hist if h["action"] == "add_format")["removed"] == [{"format": "MOBI", "backup": None}]
    # nothing to put back, so undo is refused rather than removing the only copy from Calibre
    with pytest.raises(importer.ImportError_, match="can't be undone"):
        importer.undo(conn, hist[0]["id"])
    assert importer.undo_batch(conn, 11) == {"undone": 0, "errors": []}
    assert len(_books(calibre)) == 5
