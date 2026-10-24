"""Book covers from Open Library.

Covers are fetched by cover id (the catalog's works.cover_id), which Open Library serves without a rate limit,
and by ISBN as a fallback, which it limits (about 100 per 5 minutes per IP), so those are spaced out. Downloads
are cached in /config/cache/covers. Only books that have no cover are ever touched.
"""
import time
from pathlib import Path

import httpx

from . import db, library
from .calibre_cli import Calibre, CalibreCLIError

BY_ID = "https://covers.openlibrary.org/b/id/{id}-L.jpg?default=false"
BY_ISBN = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
ISBN_GAP = 3.1            # seconds between ISBN lookups (Open Library's limit is ~100 per 5 minutes)
MIN_BYTES = 2_000         # anything smaller is a placeholder, not a cover
UA = {"User-Agent": "tomearr (self-hosted ebook manager)"}


class CoversCancelled(Exception):
    pass


def _is_image(b: bytes) -> bool:
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:4] == b"GIF8" or b[8:12] == b"WEBP"


def fetch(url: str, client: httpx.Client | None = None) -> bytes | None:
    try:
        c = client or httpx.Client(timeout=30, follow_redirects=True, headers=UA)
        r = c.get(url)
        if r.status_code != 200:
            return None
        b = r.content
        return b if len(b) >= MIN_BYTES and _is_image(b) else None
    except httpx.HTTPError:
        return None


class Fetcher:
    """Finds a cover for a work (or ISBN), with an on-disk cache and ISBN throttling."""

    def __init__(self, cache_dir: Path, client: httpx.Client | None = None, fetch_fn=None):
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=30, follow_redirects=True, headers=UA)
        self.fetch = fetch_fn
        self._last_isbn = 0.0

    def _cached(self, name: str, url: str, throttle: bool = False) -> Path | None:
        p = self.cache / name
        miss = self.cache / (name + ".none")
        if p.is_file():
            return p
        if miss.is_file() and time.time() - miss.stat().st_mtime < 30 * 86400:
            return None          # looked last month and Open Library had nothing
        if throttle:
            wait = ISBN_GAP - (time.monotonic() - self._last_isbn)
            if wait > 0:
                time.sleep(wait)
            self._last_isbn = time.monotonic()
        b = (self.fetch or fetch)(url, self.client)
        if not b:
            miss.touch()
            return None
        p.write_bytes(b)
        return p

    def for_work(self, conn, work_key: str | None, isbn: str | None = None) -> Path | None:
        if work_key:
            row = conn.execute("SELECT cover_id FROM works WHERE ol_key=?", (work_key,)).fetchone()
            if row and row[0]:
                p = self._cached(f"id-{row[0]}.jpg", BY_ID.format(id=row[0]))
                if p:
                    return p
        if isbn:
            return self._cached(f"isbn-{isbn}.jpg", BY_ISBN.format(isbn=isbn), throttle=True)
        return None


def missing(conn) -> dict:
    """Books in Calibre without a cover, and how many Tomearr has something to try for."""
    total = conn.execute("SELECT COUNT(*) FROM library_books WHERE has_cover = 0").fetchone()[0]
    rows = conn.execute(
        "SELECT b.calibre_id, b.title, b.authors, b.work_key, b.isbn, w.cover_id FROM library_books b "
        "LEFT JOIN works w ON w.ol_key = b.work_key WHERE b.has_cover = 0 "
        "AND (w.cover_id IS NOT NULL OR b.isbn IS NOT NULL) ORDER BY b.author_sort, b.title").fetchall()
    return {"missing": total, "fixable": len(rows), "items": [dict(r) for r in rows]}


def fix_missing(conn, settings, target, progress, cancelled, fetcher: Fetcher | None = None,
                batch: int | None = None) -> dict:
    """Set an Open Library cover on every Calibre book that has none."""
    from .importer import _log   # import log, so cover fixes show in the Import history
    items = missing(conn)["items"]
    fetcher = fetcher or Fetcher(Path(settings.config_dir) / "cache" / "covers")
    cal = Calibre(target)
    fixed = not_found = failed = 0
    for n, it in enumerate(items):
        if cancelled():
            raise CoversCancelled()
        progress({"step": "fetching covers", "done": n, "total": len(items), "fixed": fixed})
        img = fetcher.for_work(conn, it["work_key"], it["isbn"])
        if not img:
            not_found += 1
            continue
        try:
            book = cal.book(it["calibre_id"])
            if book is None:
                raise CalibreCLIError(f"book #{it['calibre_id']} is no longer in Calibre")
            cal.set_fields(it["calibre_id"], {"cover": str(img)})
            conn.execute("UPDATE library_books SET has_cover=1 WHERE calibre_id=?", (it["calibre_id"],))
            _log(conn, batch=batch, action="set_cover", calibre_id=it["calibre_id"], work_key=it["work_key"],
                 status="done")
            fixed += 1
        except CalibreCLIError as exc:
            _log(conn, batch=batch, action="set_cover", calibre_id=it["calibre_id"], work_key=it["work_key"],
                 status="failed", error=str(exc)[:500])
            failed += 1
        conn.commit()
    res = {"step": "done", "done": len(items), "total": len(items), "fixed": fixed, "not_found": not_found,
           "failed": failed}
    db.set_meta(conn, "covers_fixed_at", conn.execute("SELECT datetime('now')").fetchone()[0])
    progress(res)
    return res


def calibre_root(conn) -> Path:
    return Path(library.calibre_path(conn))
