"""Cleaning up leftovers (OPF, images, short txt, empty folders) after books leave the ingest folder."""
import os
import time
from pathlib import Path

import pytest

from app import calibre_cli, db, importer, ingest, leftovers, library
from app.config import get_settings
from samples import make_epub
from test_catalog import _no, _noop, conn  # noqa: F401

pytestmark = pytest.mark.skipif(calibre_cli.tool("calibredb") is None, reason="Calibre is not installed")


def _age(root, secs=3600):
    old = time.time() - secs
    for dp, _d, fs in os.walk(root):
        for f in fs:
            os.utime(os.path.join(dp, f), (old, old))


def _files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


def _dirs(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_dir())


@pytest.fixture()
def setup(env, conn, monkeypatch):  # noqa: F811
    import subprocess
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(env / "ccfg"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    lib = env / "Calibre Library"
    lib.mkdir()
    (env / "seed.txt").write_text("x" * 50)
    subprocess.run(["calibredb", "add", "--title", "Seed", "--authors", "Nobody", str(env / "seed.txt"),
                    "--library-path", str(lib)], check=True, capture_output=True,
                   env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    d = env / "ingest"
    rel = d / "Fantasy/J.R.R. Tolkien - The Hobbit [retail]"
    make_epub(rel / "The Hobbit - J.R.R. Tolkien.epub", isbn="0261102214")
    (rel / "metadata.opf").write_text("<package/>")
    (rel / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 5000)
    (rel / "info.nfo").write_text("release notes")
    (rel / "readme.txt").write_text("thanks for downloading")
    (rel / ".DS_Store").write_bytes(b"\0" * 10)
    (rel / "Covers").mkdir()
    (rel / "Covers/back.png").write_bytes(b"\x89PNG" + b"0" * 100)
    # an audiobook's mp3 sits next to this book: unknown to Tomearr, so this folder must stay
    make_epub(d / "Audio/Hamlet/hamlet.epub", title="Hamlet", authors=["William Shakespeare"])
    (d / "Audio/Hamlet/hamlet.mp3").write_bytes(b"ID3" + b"0" * 100)
    (d / "Audio/Hamlet/metadata.opf").write_text("<package/>")
    # two books in one folder; only one gets imported
    make_epub(d / "Two/lotr.epub", title="The Lord of the Rings", authors=["J.R.R. Tolkien"])
    make_epub(d / "Two/unknown book.epub", title="Quarterly Notes", authors=["Nobody At All"])
    (d / "Two/cover.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    # leftovers from before this feature existed, and an empty folder
    (d / "Old release/Covers").mkdir(parents=True)
    (d / "Old release/metadata.opf").write_text("<package/>")
    (d / "Old release/Covers/cover.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "Empty").mkdir()
    (d / "root-notes.txt").write_text("a note in the ingest folder itself")
    _age(d)
    for k, v in {"calibre_path": str(lib), "ingest_path": str(d)}.items():
        db.set_meta(conn, f"setting_{k}", v)
    library.sync(conn, env / "config" / "cache", _noop, _no)
    ingest.scan(conn, get_settings(), _noop, _no, n_workers=0)
    return d


def test_is_junk(tmp_path):
    for n in ("metadata.opf", "cover.JPG", "x.nfo", "Thumbs.db", ".DS_Store", "._book.epub", "a.sfv", "page.html"):
        (tmp_path / n).write_bytes(b"x")
        assert leftovers.is_junk(tmp_path / n), n
    (tmp_path / "short.txt").write_text("hi")
    (tmp_path / "novel.txt").write_text("word " * 10_000)
    assert leftovers.is_junk(tmp_path / "short.txt") and not leftovers.is_junk(tmp_path / "novel.txt")
    for n in ("book.epub", "book.mobi", "a.zip", "song.mp3", "film.mkv", "dl.epub.part"):
        assert not leftovers.is_junk(tmp_path / n), n


def test_plan_lists_leftover_folders(conn, setup):  # noqa: F811
    cl = {i["path"]: i for i in importer.plan(conn)["items"] if i["kind"] == "cleanup"}
    assert set(cl) == {"Old release/", "Empty/"}
    assert cl["Old release/"]["files"] == 2 and "opf" in cl["Old release/"]["steps"][0]


def test_import_cleans_up_its_folder(conn, setup, env):  # noqa: F811
    d = setup
    keys = [i["key"] for i in importer.plan(conn)["items"] if i.get("path", "").startswith(("Fantasy/", "Audio/"))
            or i.get("path") == "Two/lotr.epub"]
    r = importer.run(conn, get_settings(), _noop, _no, keys=keys, batch=31)
    assert (r["done"], r["failed"]) == (3, 0), [h["error"] for h in importer.history(conn) if h["error"]]
    assert _files(d) == ["Audio/Hamlet/hamlet.mp3", "Audio/Hamlet/metadata.opf", "Old release/Covers/cover.jpg",
                         "Old release/metadata.opf", "Two/cover.jpg", "Two/unknown book.epub", "root-notes.txt"]
    # the release folder and its now-empty parent are gone; the ingest folder itself stays
    assert not (d / "Fantasy").exists() and d.is_dir()
    hob = next(h for h in importer.history(conn) if h["path"].startswith("Fantasy/"))
    assert len(hob["meta"]["leftovers"]) == 6
    trash = next((env / "config" / "trash").glob("*-job31"))
    assert (trash / "ingest/Fantasy/J.R.R. Tolkien - The Hobbit [retail]/metadata.opf").is_file()
    # undo brings the book and its leftovers back exactly where they were
    importer.undo(conn, hob["id"])
    rel = d / "Fantasy/J.R.R. Tolkien - The Hobbit [retail]"
    assert sorted(p.name for p in rel.rglob("*") if p.is_file()) == \
        [".DS_Store", "The Hobbit - J.R.R. Tolkien.epub", "back.png", "cover.jpg", "info.nfo", "metadata.opf",
         "readme.txt"]


def test_sweep_old_leftovers_and_undo(conn, setup):  # noqa: F811
    d = setup
    keys = [i["key"] for i in importer.plan(conn)["items"] if i["kind"] == "cleanup"]
    r = importer.run(conn, get_settings(), _noop, _no, keys=keys, batch=32)
    assert r["done"] == 2
    assert not (d / "Old release").exists() and not (d / "Empty").exists()
    assert (d / "root-notes.txt").is_file()                      # the ingest folder's own files are left alone
    assert importer.undo_batch(conn, 32)["undone"] == 2
    assert (d / "Old release/Covers/cover.jpg").is_file() and (d / "Old release/metadata.opf").is_file()


def test_recent_files_and_trash_off(conn, setup):  # noqa: F811
    d = setup
    (d / "Old release/new.jpg").write_bytes(b"x")                 # just arrived: leave the folder for now
    assert "Old release/" not in {i["path"] for i in importer.plan(conn)["items"] if i["kind"] == "cleanup"}
    db.set_meta(conn, "setting_trash_days", "0")
    keys = [i["key"] for i in importer.plan(conn)["items"] if i.get("path", "").startswith("Fantasy/")]
    importer.run(conn, get_settings(), _noop, _no, keys=keys, batch=33)
    assert not (d / "Fantasy").exists()
    h = importer.history(conn)[0]
    assert h["meta"]["no_undo"] and all(x["trash"] is None for x in h["meta"]["leftovers"])
