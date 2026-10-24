"""Covers from Open Library: fix coverless Calibre books, and embed a cover when an imported EPUB has none."""
import os
import struct
import subprocess
import time
import zlib

import pytest

from app import calibre_cli, covers, db, importer, ingest, library
from app.config import get_settings
from samples import make_epub
from test_catalog import _no, _noop, conn  # noqa: F401

pytestmark = pytest.mark.skipif(calibre_cli.tool("calibredb") is None, reason="Calibre is not installed")


def png(w=300, h=450):
    """A real-looking cover: noisy pixels, so it doesn't compress down to placeholder size."""
    import random
    rnd = random.Random(w * h)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(3 * w)) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeOL:
    """Open Library's cover service: knows LOTR's cover id and The Hobbit's ISBN."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, client=None):
        self.calls.append(url)
        if "/id/9255566-" in url or "/isbn/9780261102217-" in url:
            return png()
        return None


def _cdb(lib, *args):
    return subprocess.run(["calibredb", *args, "--library-path", str(lib)], capture_output=True, text=True,
                          check=True, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}).stdout


@pytest.fixture()
def lib(env, conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(env / "ccfg"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(covers, "ISBN_GAP", 0)
    lib = env / "Calibre Library"
    lib.mkdir()
    # three coverless books: LOTR (work has a cover id), The Hobbit (ISBN only), Macbeth (nothing to go on)
    for t, a, isbn in (("The Lord of the Rings", "J.R.R. Tolkien", None), ("The Hobbit", "J.R.R. Tolkien", "9780261102217"),
                       ("Macbeth", "William Shakespeare", None)):
        f = env / "seed" / f"{t}.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("x" * 100)
        args = ["add", "--title", t, "--authors", a, str(f)] + (["--isbn", isbn] if isbn else [])
        _cdb(lib, *args)
    db.set_meta(conn, "setting_calibre_path", str(lib))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    return lib


def _covers(lib):
    return sorted(p.parent.name for p in lib.rglob("cover.jpg"))


def test_fix_missing_covers(conn, lib, env):  # noqa: F811
    # make the Hobbit an ISBN-only case: its work has no cover id in the test catalog
    m = covers.missing(conn)
    assert m["missing"] == 3 and m["fixable"] == 2          # Macbeth has neither a cover id nor an ISBN
    fake = FakeOL()
    f = covers.Fetcher(env / "config" / "cache" / "covers", fetch_fn=fake)
    r = covers.fix_missing(conn, get_settings(), importer.target(conn), _noop, _no, fetcher=f, batch=5)
    assert (r["fixed"], r["not_found"], r["failed"]) == (2, 0, 0)
    assert _covers(lib) == ["The Hobbit (2)", "The Lord of the Rings (1)"]
    assert covers.missing(conn)["missing"] == 1
    # a resync agrees with what Calibre now says
    library.sync(conn, env / "config" / "cache", _noop, _no)
    assert conn.execute("SELECT COUNT(*) FROM library_books WHERE has_cover=0").fetchone()[0] == 1
    # cached: a second run asks Open Library nothing new
    n = len(fake.calls)
    assert f.for_work(conn, "OL27448W") is not None and len(fake.calls) == n
    # a miss is remembered too, so it isn't asked again for a month
    assert f.for_work(conn, None, "9780000000002") is None and f.for_work(conn, None, "9780000000002") is None
    assert len(fake.calls) == n + 1
    # cover fixes show in the history but can't be undone through calibredb
    h = [x for x in importer.history(conn) if x["action"] == "set_cover"]
    assert len(h) == 2 and h[0]["batch"] == 5
    with pytest.raises(importer.ImportError_, match="can't remove a cover"):
        importer.undo(conn, h[0]["id"])
    assert importer.undo_batch(conn, 5) == {"undone": 0, "errors": []}


def test_only_coverless_books_are_touched(conn, lib, env):  # noqa: F811
    covers.fix_missing(conn, get_settings(), importer.target(conn), _noop, _no,
                       fetcher=covers.Fetcher(env / "c1", fetch_fn=FakeOL()))
    before = {p: p.read_bytes() for p in lib.rglob("cover.jpg")}
    r = covers.fix_missing(conn, get_settings(), importer.target(conn), _noop, _no,
                           fetcher=covers.Fetcher(env / "c2", fetch_fn=lambda u, c=None: png(10 * 30, 20 * 30)))
    assert r["fixed"] == 0                                  # only Macbeth is left, and there's nothing to fetch
    assert {p: p.read_bytes() for p in lib.rglob("cover.jpg")} == before


def test_placeholder_and_junk_are_rejected():
    assert covers._is_image(png()) and not covers._is_image(b"<html>not found</html>")

    class R:
        def __init__(self, code, content):
            self.status_code, self.content = code, content

    class C:
        def __init__(self, r):
            self.r = r

        def get(self, url):
            return self.r
    assert covers.fetch("u", C(R(404, b""))) is None
    assert covers.fetch("u", C(R(200, b"\xff\xd8\xff" + b"0" * 10))) is None   # too small: a placeholder
    assert covers.fetch("u", C(R(200, png()))) is not None


def test_import_embeds_a_cover_when_the_epub_has_none(conn, lib, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(covers, "fetch", FakeOL())
    d = env / "ingest"
    make_epub(d / "lotr.epub", title="The Lord of the Rings", authors=["J.R.R. Tolkien"])
    make_epub(d / "silm.epub", title="The Silmarillion", authors=["J.R.R. Tolkien"])   # no cover anywhere
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))
    # the library already has LOTR (coverless); remove it so this import adds a new book
    _cdb(lib, "remove", "1", "--permanent")
    library.sync(conn, env / "config" / "cache", _noop, _no)
    db.set_meta(conn, "setting_ingest_path", str(d))
    ingest.scan(conn, get_settings(), _noop, _no, n_workers=0)
    r = importer.run(conn, get_settings(), _noop, _no, batch=9)
    assert (r["done"], r["failed"]) == (2, 0), [h["error"] for h in importer.history(conn) if h["error"]]
    hist = {h["path"]: h for h in importer.history(conn) if h["action"] == "add"}
    assert hist["lotr.epub"]["meta"]["cover_added"] is True
    assert hist["silm.epub"]["meta"]["cover_added"] is False
    lotr_dir = next(p for p in lib.rglob("The Lord of the Rings (*)"))
    assert (lotr_dir / "cover.jpg").is_file()
    epub = next(lotr_dir.glob("*.epub"))
    assert calibre_cli.has_cover(epub)
    # turned off: no lookups at all
    db.set_meta(conn, "setting_fetch_covers", "0")
    assert importer.setting(conn, "fetch_covers") == "0"
