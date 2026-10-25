"""Comparing an ingest file with the EPUB Calibre already has, and replacing it if the new one is better."""
import hashlib
import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from app import calibre_cli, compare, db, importer, ingest, library
from app.config import get_settings
from samples import make_mobi
from test_catalog import _no, _noop, conn  # noqa: F401
from test_covers import png

pytestmark = pytest.mark.skipif(calibre_cli.tool("calibredb") is None, reason="Calibre is not installed")

WORDS = ("In a hole in the ground there lived a hobbit Not a nasty dirty wet hole filled with the ends of worms "
         "and an oozy smell nor yet a dry bare sandy hole with nothing in it to sit down on or to eat it was a "
         "hobbit hole and that means comfort").split()


def chapter_text(i, n_words):
    return " ".join(WORDS[(i * 7 + k) % len(WORDS)] for k in range(n_words))


def build_epub(path, title="The Hobbit", author="J.R.R. Tolkien", chapters=10, words=400, cover=True, toc=True,
               converted=False, broken_words=0):
    """A realistic EPUB: chapters of text, optional cover, nav TOC, and either publisher-style or
    calibre-conversion-style file names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = (lambda i: f"index_split_{i:03d}.html") if converted else (lambda i: f"Text/chapter{i + 1:02d}.xhtml")
    cls = ' class="calibre1"' if converted else ""
    items, spine, navli = [], [], []
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                   'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for i in range(chapters):
            body = chapter_text(i, words)
            if broken_words:
                body += " " + " ".join("exam- ple" for _ in range(broken_words // chapters + 1))
            z.writestr(f"OEBPS/{name(i)}", f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title>'
                       f'<style>p{{margin:0}}</style></head><body><h1{cls}>Chapter {i + 1}</h1>'
                       + "".join(f"<p{cls}>{body[j:j + 400]}</p>" for j in range(0, len(body), 400)) + "</body></html>")
            items.append(f'<item id="c{i}" href="{name(i)}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            navli.append(f'<li><a href="{name(i)}">Chapter {i + 1}</a></li>')
        meta_cover = ""
        if cover:
            z.writestr("OEBPS/Images/cover.png", png(60, 90))
            items.append('<item id="cover" href="Images/cover.png" media-type="image/png" properties="cover-image"/>')
            meta_cover = '<meta name="cover" content="cover"/>'
        if toc:
            z.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                       f'<body><nav epub:type="toc"><ol>{"".join(navli)}</ol></nav></body></html>')
            items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   f'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>'
                   f'<dc:creator>{author}</dc:creator><dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language>'
                   f'{meta_cover}</metadata><manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')
    return path


def _cdb(lib, *args):
    return subprocess.run(["calibredb", *args, "--library-path", str(lib)], capture_output=True, text=True, check=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}).stdout


def _lib_epub(lib, title="The Hobbit"):
    return next(Path(lib).rglob(f"{title} (*)/*.epub"))


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture()
def setup(env, conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(env / "ccfg"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    lib = env / "Calibre Library"
    lib.mkdir()
    # Calibre has a converted, coverless, shorter Hobbit
    _cdb(lib, "add", "--title", "The Hobbit", "--authors", "J.R.R. Tolkien",
         str(build_epub(env / "seed/hobbit.epub", chapters=8, words=300, cover=False, toc=False, converted=True)))
    d = env / "ingest"
    build_epub(d / "The Hobbit - J.R.R. Tolkien (retail).epub", chapters=10, words=400)     # better
    build_epub(d / "hobbit sample/The Hobbit - J.R.R. Tolkien.epub", chapters=2, words=300, cover=False)  # worse
    make_mobi(d / "The Hobbit - J.R.R. Tolkien.azw3", title="The Hobbit", author="J.R.R. Tolkien", kf8=True)
    old = time.time() - 3600
    for dp, _d, fs in os.walk(d):
        for f in fs:
            os.utime(os.path.join(dp, f), (old, old))
    for k, v in {"calibre_path": str(lib), "ingest_path": str(d), "delete_duplicates": "0"}.items():
        db.set_meta(conn, f"setting_{k}", v)
    library.sync(conn, env / "config" / "cache", _noop, _no)
    ingest.scan(conn, get_settings(), _noop, _no, n_workers=0)
    ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM ingest_files")}
    return lib, d, ids


def test_inspect_measures_an_epub(tmp_path):
    good = compare.inspect_epub(build_epub(tmp_path / "a.epub", chapters=10, words=400))
    assert (good["chapters"], good["has_cover"], good["toc_entries"], good["epub_version"]) == (10, True, 10, "3.0")
    assert 4000 <= good["words"] <= 4100        # 4,000 words of text plus chapter headings
    assert good["origin"] == "publisher" and good["images"] == 1 and good["meta"]["title"] == "The Hobbit"
    conv = compare.inspect_epub(build_epub(tmp_path / "b.epub", converted=True, cover=False, toc=False))
    assert conv["origin"] == "converted" and not conv["has_cover"] and conv["toc_entries"] == 0
    ocr = compare.inspect_epub(build_epub(tmp_path / "c.epub", broken_words=200))
    assert ocr["origin"] == "converted" and any("OCR" in w for w in ocr["origin_why"])
    (tmp_path / "bad.epub").write_bytes(b"nope")
    assert compare.inspect_epub(tmp_path / "bad.epub")["error"]


def test_plan_shows_already_have(conn, setup):  # noqa: F811
    lib, d, ids = setup
    kinds = {i["path"]: (i["kind"], i["select"]) for i in importer.plan(conn)["items"]}
    assert set(kinds.values()) == {("already_have", False)} and len(kinds) == 3


def test_compare_suggests_the_better_copy(conn, setup):  # noqa: F811
    lib, d, ids = setup
    s = get_settings()
    better = compare.compare(conn, s, ids["The Hobbit - J.R.R. Tolkien (retail).epub"])
    assert better["suggestion"]["choice"] == "ingest"
    why = " ".join(better["suggestion"]["why"])
    assert "longer" in why and "publisher-made" in why and "cover" in why and "table of contents" in why
    assert better["calibre"]["origin"] == "converted" and better["ingest"]["origin"] == "publisher"
    assert better["ingest"]["cover_url"] and better["calibre"]["cover_url"] is None
    # the passage is the same text from both books
    assert better["calibre"]["sample"].split()[:8] == better["ingest"]["sample"].split()[:8]
    worse = compare.compare(conn, s, ids["hobbit sample/The Hobbit - J.R.R. Tolkien.epub"])
    assert worse["suggestion"]["choice"] == "calibre" and "shorter" in " ".join(worse["suggestion"]["why"])
    azw = compare.compare(conn, s, ids["The Hobbit - J.R.R. Tolkien.azw3"])
    assert azw["converted_for_compare"] and azw["ingest"]["origin"] == "unknown"
    # cached: the second call doesn't convert again
    t = time.time()
    compare.compare(conn, s, ids["The Hobbit - J.R.R. Tolkien.azw3"])
    assert time.time() - t < 0.5
    assert compare.cover_path(s, ids["The Hobbit - J.R.R. Tolkien (retail).epub"], "ingest").is_file()
    with pytest.raises(KeyError):
        compare.compare(conn, s, 99999)


def test_replace_and_undo(conn, setup, env):  # noqa: F811
    lib, d, ids = setup
    s = get_settings()
    fid = ids["The Hobbit - J.R.R. Tolkien (retail).epub"]
    old_sha = _sha(_lib_epub(lib))
    old_words = compare.inspect_epub(_lib_epub(lib))["words"]
    importer.set_compare_choice(conn, fid, "ingest")
    importer.set_compare_choice(conn, ids["hobbit sample/The Hobbit - J.R.R. Tolkien.epub"], "calibre")
    importer.set_compare_choice(conn, ids["The Hobbit - J.R.R. Tolkien.azw3"], "skip")
    items = {i["path"]: i for i in importer.plan(conn)["items"]}
    assert items["The Hobbit - J.R.R. Tolkien (retail).epub"]["kind"] == "replace"
    assert items["hobbit sample/The Hobbit - J.R.R. Tolkien.epub"]["kind"] == "already_have"
    assert items["hobbit sample/The Hobbit - J.R.R. Tolkien.epub"]["select"] is True
    assert items["The Hobbit - J.R.R. Tolkien.azw3"]["select"] is False
    r = importer.run(conn, s, _noop, _no, batch=21)
    assert (r["done"], r["failed"]) == (2, 0), [h["error"] for h in importer.history(conn) if h["error"]]
    # Calibre's EPUB is now the better one; still one book, one EPUB
    new = compare.inspect_epub(_lib_epub(lib))
    assert new["words"] > old_words and new["has_cover"] and new["origin"] == "publisher"
    assert len(list(lib.rglob("*.epub"))) == 1
    # Calibre's record had no cover, so it took the new EPUB's
    assert list(lib.rglob("The Hobbit (*)/cover.jpg"))
    assert next(h for h in importer.history(conn) if h["action"] == "replace")["meta"]["calibre_cover_set"] is True
    left = {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()}
    assert left == {"The Hobbit - J.R.R. Tolkien.azw3"}                     # skipped one stays
    # undo puts Calibre's old EPUB back and the file back in the ingest folder
    assert importer.undo_batch(conn, 21) == {"undone": 2, "errors": []}
    assert _sha(_lib_epub(lib)) == old_sha
    assert (d / "The Hobbit - J.R.R. Tolkien (retail).epub").is_file()


def test_replace_with_trash_off(conn, setup):  # noqa: F811
    lib, d, ids = setup
    db.set_meta(conn, "setting_trash_days", "0")
    importer.set_compare_choice(conn, ids["The Hobbit - J.R.R. Tolkien (retail).epub"], "ingest")
    r = importer.run(conn, get_settings(), _noop, _no, keys=[f"file:{ids['The Hobbit - J.R.R. Tolkien (retail).epub']}"],
                     batch=22)
    assert r["done"] == 1
    h = importer.history(conn)[0]
    assert h["action"] == "replace" and h["meta"]["no_undo"] and h["removed"] == [{"format": "EPUB", "backup": None}]
    with pytest.raises(importer.ImportError_, match="can't be undone"):
        importer.undo(conn, h["id"])


def test_choice_validation_and_rescan(conn, setup):  # noqa: F811
    lib, d, ids = setup
    with pytest.raises(importer.ImportError_):
        importer.set_compare_choice(conn, ids["The Hobbit - J.R.R. Tolkien.azw3"], "both")
    with pytest.raises(KeyError):
        importer.set_compare_choice(conn, 99999, "skip")
    importer.set_compare_choice(conn, ids["The Hobbit - J.R.R. Tolkien.azw3"], "skip")
    ingest.scan(conn, get_settings(), _noop, _no, n_workers=0, full=True)
    assert conn.execute("SELECT compare_choice FROM ingest_files WHERE id=?",
                        (ids["The Hobbit - J.R.R. Tolkien.azw3"],)).fetchone()[0] == "skip"


def test_api_compare(env, setup, conn):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import create_app
    lib, d, ids = setup
    fid = ids["The Hobbit - J.R.R. Tolkien (retail).epub"]
    with TestClient(create_app()) as client:
        c = client.get(f"/api/import/compare/{fid}").json()
        assert c["suggestion"]["choice"] == "ingest" and c["choice"] is None
        assert client.get(c["ingest"]["cover_url"]).headers["content-type"] == "image/jpeg"
        assert client.get(f"/api/import/compare/{fid}/cover/calibre").status_code == 404
        assert client.post(f"/api/import/compare/{fid}/choice", json={"choice": "ingest"}).json()["choice"] == "ingest"
        assert client.get(f"/api/import/compare/{fid}").json()["choice"] == "ingest"
        assert client.post(f"/api/import/compare/{fid}/choice", json={"choice": "nope"}).status_code == 400
        assert client.get("/api/import/compare/99999").status_code == 404
        p = client.get("/api/import/plan").json()
        assert next(i for i in p["items"] if i.get("file_id") == fid)["kind"] == "replace"
