"""Phase 4: scan the ingest folder and identify every file in it. Report only: nothing is moved or changed.

A scan:
  1. walks the folder (skipping files modified in the last `settle` seconds, which are probably still arriving),
  2. reads new or changed files in worker processes (format, embedded metadata, DRM, SHA-256), each with a time
     limit so one broken file can't stall the scan,
  3. matches each file to a catalog work, trying the embedded metadata, a metadata.opf sidecar and several readings
     of the file name, and keeping whichever matches best,
  4. sorts files into buckets and finds duplicates within the folder and against Calibre (by content hash).
Unchanged files (same size and mtime) are not read again, so frequent interval scans are cheap.
"""
import multiprocessing as mp
import os
import sqlite3
import stat as statmod
import time
from pathlib import Path

import orjson

from . import db, extract, filenames, matcher

DEFAULT_INGEST_PATH = os.environ.get("TOMEARR_INGEST", "/ingest")
DEFAULTS = {"ingest_path": DEFAULT_INGEST_PATH, "ingest_interval_min": "5", "ingest_settle_sec": "120",
            "library_sync_interval_min": "60"}
SKIP_DIRS = {"@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found", ".Trash", ".Trashes", "#snapshot"}
MATCHED, REVIEW = 90, matcher.MIN_CONFIDENCE
BUCKETS = ("matched", "review", "unmatched", "duplicate", "drm", "archive", "comic", "error")


class ScanCancelled(Exception):
    pass


def setting(conn: sqlite3.Connection, key: str) -> str:
    return db.get_meta(conn, f"setting_{key}", DEFAULTS[key])


def workers() -> int:
    env = os.environ.get("TOMEARR_WORKERS")
    if env is not None:
        return max(0, int(env))
    return max(1, min(4, (os.cpu_count() or 2) - 1))


# --- 1. walk ----------------------------------------------------------------------------------------------------

def walk(root: Path, settle: int) -> tuple[dict[str, tuple[int, int, str]], dict]:
    """{relpath: (size, mtime_ns, class)} for candidate files, plus counts of what was skipped."""
    found: dict[str, tuple[int, int, str]] = {}
    counts = {"settling": 0, "ignored": 0, "partial": 0, "small_txt": 0}
    cutoff = time.time_ns() - settle * 1_000_000_000
    for dirpath, dirnames, files in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            cls = extract.classify_ext(f)
            if cls in ("ignored", "sidecar"):
                counts["ignored"] += cls == "ignored"
                continue
            full = Path(dirpath) / f
            try:
                st = full.stat()
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            if cls == "partial":
                counts["partial"] += 1
                continue
            if f.lower().endswith(".txt") and st.st_size < extract.MIN_TXT_SIZE:
                counts["small_txt"] += 1
                continue
            if st.st_mtime_ns > cutoff:
                counts["settling"] += 1
                continue
            found[full.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns, cls)
    return found, counts


# --- 2. read files in worker processes ---------------------------------------------------------------------------

def _timeout_for(size: int) -> float:
    return 60 + size / (10 * 1024 * 1024)      # a minute, plus a second per 10 MB for hashing


def read_files(root: Path, rels: list[str], sizes: dict[str, int], cancelled, n_workers: int | None = None):
    """Yield (relpath, analyze() result). A file that exceeds its time limit yields an error result and the
    stuck worker is replaced."""
    n = workers() if n_workers is None else n_workers
    if n == 0 or len(rels) <= 2:
        for rel in rels:
            if cancelled():
                raise ScanCancelled()
            yield rel, extract.analyze(root / rel)
        return
    ctx = mp.get_context("forkserver")
    pool = ctx.Pool(n, maxtasksperchild=500)
    pending = list(reversed(rels))
    window: list[tuple[str, object, float]] = []
    try:
        while pending or window:
            while pending and len(window) < n * 2:
                rel = pending.pop()
                window.append((rel, pool.apply_async(extract.analyze, (str(root / rel),)),
                               time.monotonic() + _timeout_for(sizes.get(rel, 0))))
            if cancelled():
                raise ScanCancelled()
            rel, res, deadline = window[0]
            try:
                out = res.get(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
            except mp.TimeoutError:
                if time.monotonic() < deadline:
                    continue                      # not late yet; check cancel and wait again
                pool.terminate()
                pool = ctx.Pool(n, maxtasksperchild=500)
                pending.extend(r for r, _, _ in reversed(window[1:]))
                window = []
                yield rel, {"kind": "ebook", "error": "timed out reading this file", "size": sizes.get(rel)}
                continue
            window.pop(0)
            yield rel, out
    finally:
        pool.terminate()


# --- 3. match -----------------------------------------------------------------------------------------------------

def readings(meta: dict | None, sidecar: dict | None, relpath: str) -> list[tuple[str, str, list[str]]]:
    """(source, title, authors) guesses, most trustworthy first."""
    out: list[tuple[str, str, list[str]]] = []
    fn = filenames.hypotheses(relpath)
    fn_authors = next((h["authors"] for h in fn if h["authors"]), [])
    if sidecar and sidecar.get("title"):
        out.append(("sidecar", sidecar["title"], sidecar.get("authors") or fn_authors))
    if meta and meta.get("title"):
        out.append(("embedded", meta["title"], meta.get("authors") or []))
        if not meta.get("authors") and fn_authors:
            out.append(("embedded+filename", meta["title"], fn_authors))
    for h in fn:
        out.append((f"filename:{h['how']}", h["title"], h["authors"]))
    seen, uniq = set(), []
    for o in out:
        key = (o[1].lower(), tuple(a.lower() for a in o[2]))
        if key not in seen:
            seen.add(key)
            uniq.append(o)
    return uniq[:8]


def match_file(conn, meta: dict | None, sidecar: dict | None, relpath: str) -> dict:
    isbns = list(dict.fromkeys((sidecar or {}).get("isbns", []) + (meta or {}).get("isbns", [])))
    isbn = isbns[0] if isbns else None
    tries = readings(meta, sidecar, relpath)
    if isbn and not any(src in ("sidecar", "embedded") for src, _t, _a in tries):
        # The ISBN came from the file itself (e.g. a PDF's copyright page) and nothing in the file names a title.
        # A guess from the file name is weaker evidence than the ISBN, so look the ISBN up on its own first.
        tries.insert(0, ("isbn", "", []))
    tries = tries or [("isbn", "", [])]
    best = None
    for src, title, authors in tries:
        m = matcher.match_book(conn, title, authors, isbn=isbn, deep=False)
        if best is None or m.confidence > best[3].confidence:
            best = (src, title, authors, m)
        if m.confidence >= 95:
            break
    if best[3].confidence < MATCHED:
        # Still unsure: give the most trustworthy reading that names an author one deep look (edition titles).
        src, title, authors = next(((s, t, a) for s, t, a in tries if a and t), best[:3])
        m = matcher.match_book(conn, title, authors, isbn=isbn, deep=True)
        if m.confidence > best[3].confidence:
            best = (src, title, authors, m)
    src, title, authors, m = best
    cands = [{"ol_key": c["ol_key"], "title": c["title"], "year": c.get("first_publish_year"),
              "authors": [a["name"] for a in c.get("authors", [])], "score": c.get("score")}
             for c in m.candidates[:5]]
    return {"title": title or None, "authors": " & ".join(matcher.split_authors(" ; ".join(authors))) or None,
            "isbn": isbn, "match_source": src if m.work_key else None, "work_key": m.work_key,
            "match_method": m.method, "match_confidence": m.confidence,
            "candidates_json": orjson.dumps(cands).decode(), "issues": ",".join(m.issues) or None}


def base_bucket(r) -> str:
    if r["kind"] == "archive":
        return "archive"
    if r["kind"] == "comic":
        return "comic"
    if r["drm"]:
        return "drm"
    if r["error"] and not r["meta_json"] and not r["work_key"]:
        return "error"
    if r["work_key"] and r["match_confidence"] >= MATCHED:
        return "matched"
    if r["work_key"] and r["match_confidence"] >= REVIEW:
        return "review"
    return "unmatched"


# --- 4. duplicates and buckets ------------------------------------------------------------------------------------

def _hash(path: Path) -> str | None:
    try:
        return extract.sha256_file(path)
    except OSError:
        return None


def assign_buckets(conn, calibre_root: Path | None, progress) -> None:
    progress({"step": "checking for duplicates"})
    rows = conn.execute("SELECT id, path, size, sha256, kind, drm, error, meta_json, work_key, match_confidence "
                        "FROM ingest_files ORDER BY first_seen, path").fetchall()
    first_by_hash: dict[str, str] = {}
    sizes = {r["size"] for r in rows if r["size"]}
    cal_by_size: dict[int, list] = {}
    if sizes:
        for cr in conn.execute("SELECT calibre_id, format, size, relpath, sha256 FROM calibre_files WHERE size IN "
                               f"(SELECT size FROM ingest_files)"):
            cal_by_size.setdefault(cr["size"], []).append(dict(cr))
    owned: dict[str, set] = {}
    for wk, f in conn.execute("SELECT work_key, formats FROM library_books WHERE work_key IN "
                              "(SELECT work_key FROM ingest_files WHERE work_key IS NOT NULL)"):
        owned.setdefault(wk, set()).update(x for x in (f or "").split(",") if x)

    updates = []
    for r in rows:
        bucket, dup = base_bucket(r), None
        h = r["sha256"]
        if h and h in first_by_hash:
            bucket, dup = "duplicate", f"ingest:{first_by_hash[h]}"
        elif h:
            first_by_hash[h] = r["path"]
            for cf in cal_by_size.get(r["size"], []):
                if cf["sha256"] is None and calibre_root is not None:
                    cf["sha256"] = _hash(calibre_root / cf["relpath"])
                    conn.execute("UPDATE calibre_files SET sha256=? WHERE calibre_id=? AND format=?",
                                 (cf["sha256"], cf["calibre_id"], cf["format"]))
                if cf["sha256"] == h:
                    bucket, dup = "duplicate", f"calibre:{cf['calibre_id']}"
                    break
        of = owned.get(r["work_key"])
        updates.append((bucket, dup, ",".join(sorted(of)) if of else None, r["id"]))
    conn.executemany("UPDATE ingest_files SET bucket=?, duplicate_of=?, owned_formats=? WHERE id=?", updates)
    conn.commit()


# --- the scan job -------------------------------------------------------------------------------------------------

UPSERT = """
INSERT INTO ingest_files(path, size, mtime_ns, sha256, kind, format, drm, drm_detail, meta_json, meta_source,
    sidecar_json, entries_json, error, scanned_at)
VALUES(:path, :size, :mtime_ns, :sha256, :kind, :format, :drm, :drm_detail, :meta_json, :meta_source,
    :sidecar_json, :entries_json, :error, :now)
ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
    kind=excluded.kind, format=excluded.format, drm=excluded.drm, drm_detail=excluded.drm_detail,
    meta_json=excluded.meta_json, meta_source=excluded.meta_source, sidecar_json=excluded.sidecar_json,
    entries_json=excluded.entries_json, error=excluded.error, scanned_at=excluded.scanned_at,
    -- a review decision belongs to the exact file it was made on: new contents, new decision
    decision=CASE WHEN excluded.sha256 IS ingest_files.sha256 THEN ingest_files.decision END,
    decided_work_key=CASE WHEN excluded.sha256 IS ingest_files.sha256 THEN ingest_files.decided_work_key END,
    override_json=CASE WHEN excluded.sha256 IS ingest_files.sha256 THEN ingest_files.override_json END,
    decided_at=CASE WHEN excluded.sha256 IS ingest_files.sha256 THEN ingest_files.decided_at END
"""
MATCH_SQL = ("UPDATE ingest_files SET title=:title, authors=:authors, isbn=:isbn, match_source=:match_source, "
             "work_key=:work_key, match_method=:match_method, match_confidence=:match_confidence, "
             "candidates_json=:candidates_json, issues=:issues WHERE path=:path")


def _rematch_row(conn, r) -> dict:
    meta = orjson.loads(r["meta_json"]) if r["meta_json"] else None
    side = orjson.loads(r["sidecar_json"]) if r["sidecar_json"] else None
    if r["kind"] != "ebook":
        return {"path": r["path"], "title": None, "authors": None, "isbn": None, "match_source": None,
                "work_key": None, "match_method": None, "match_confidence": 0, "candidates_json": None, "issues": None}
    return {"path": r["path"], **match_file(conn, meta, side, r["path"])}


def scan(conn: sqlite3.Connection, settings, progress, cancelled, full: bool = False, rematch: bool = False,
         n_workers: int | None = None) -> dict:
    root = Path(setting(conn, "ingest_path"))
    if not root.is_dir():
        raise FileNotFoundError(f"Ingest folder {root} does not exist (is it mounted?)")
    settle = int(setting(conn, "ingest_settle_sec"))
    progress({"step": "listing files"})
    found, counts = walk(root, settle)

    prev = {r["path"]: (r["size"], r["mtime_ns"]) for r in conn.execute("SELECT path, size, mtime_ns FROM ingest_files")}
    gone = [p for p in prev if p not in found]
    for i in range(0, len(gone), 500):
        chunk = gone[i:i + 500]
        conn.execute(f"DELETE FROM ingest_files WHERE path IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()

    todo = [rel for rel, (size, mtime, _c) in found.items() if full or prev.get(rel) != (size, mtime)]
    todo.sort()
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    total, done = len(todo), 0
    progress({"step": "reading files", "done": 0, "total": total})
    for rel, res in read_files(root, todo, {r: found[r][0] for r in todo}, cancelled, n_workers):
        size, mtime, cls = found[rel]
        full_path = root / rel
        sidecar = extract.read_sidecar(full_path.parent, full_path.stem) if res.get("kind") == "ebook" else None
        row = {
            "path": rel, "size": res.get("size") or size, "mtime_ns": mtime, "sha256": res.get("sha256"),
            "kind": res.get("kind") or cls, "format": res.get("format"),
            "drm": None if res.get("drm") is None else int(bool(res["drm"])), "drm_detail": res.get("drm_detail"),
            "meta_json": orjson.dumps(res["meta"]).decode() if res.get("meta") else None,
            "meta_source": res.get("meta_source"),
            "sidecar_json": orjson.dumps(sidecar).decode() if sidecar else None,
            "entries_json": orjson.dumps(res["entries"]).decode() if res.get("entries") else None,
            "error": res.get("error"), "now": now,
        }
        conn.execute(UPSERT, row)
        r = conn.execute("SELECT * FROM ingest_files WHERE path=?", (rel,)).fetchone()
        conn.execute(MATCH_SQL, _rematch_row(conn, r))
        done += 1
        if done % 20 == 0:
            conn.commit()
            progress({"step": "reading files", "done": done, "total": total})
    conn.commit()

    rematched = 0
    if rematch:
        rows = conn.execute("SELECT * FROM ingest_files WHERE path NOT IN (%s)" % ",".join("?" * len(todo)),
                            todo).fetchall() if todo else conn.execute("SELECT * FROM ingest_files").fetchall()
        for i, r in enumerate(rows):
            if i % 50 == 0:
                if cancelled():
                    raise ScanCancelled()
                progress({"step": "re-matching", "done": i, "total": len(rows)})
            conn.execute(MATCH_SQL, _rematch_row(conn, r))
            rematched += 1
        conn.commit()

    cal = Path(db.get_meta(conn, "setting_calibre_path", "/calibre"))
    assign_buckets(conn, cal if (cal / "metadata.db").exists() else None, progress)
    result = {"step": "done", "files": len(found), "read": done, "removed": len(gone), "rematched": rematched,
              **counts}
    db.set_meta(conn, "ingest_scanned_at", now)
    db.set_meta(conn, "ingest_last_scan", orjson.dumps(result).decode())
    progress(result)
    return result


# --- queries for the API ----------------------------------------------------------------------------------------

# Files waiting for a person: the matcher wasn't sure, and nobody has decided yet.
QUEUE_SQL = "bucket IN ('review', 'unmatched') AND decision IS NULL AND kind = 'ebook'"
# Files the import step (Phase 6) may take: accepted by you, or matched confidently and not overruled.
READY_SQL = ("bucket NOT IN ('duplicate', 'drm', 'archive', 'comic', 'error') AND kind = 'ebook' AND "
             "(decision = 'accepted' OR (decision IS NULL AND bucket = 'matched'))")


def stats(conn: sqlite3.Connection) -> dict:
    by = {b: 0 for b in BUCKETS}
    for b, n in conn.execute("SELECT bucket, COUNT(*) FROM ingest_files GROUP BY bucket"):
        by[b or "unmatched"] = n
    formats = {f: n for f, n in conn.execute(
        "SELECT format, COUNT(*) FROM ingest_files WHERE format IS NOT NULL GROUP BY format ORDER BY 2 DESC")}
    dec = {d or "pending": n for d, n in conn.execute("SELECT decision, COUNT(*) FROM ingest_files GROUP BY decision")}
    return {
        "files": sum(by.values()), "buckets": by, "formats": formats, "decisions": dec,
        "review_queue": conn.execute(f"SELECT COUNT(*) FROM ingest_files WHERE {QUEUE_SQL}").fetchone()[0],
        "ready": conn.execute(f"SELECT COUNT(*) FROM ingest_files WHERE {READY_SQL}").fetchone()[0],
        "owned": conn.execute("SELECT COUNT(*) FROM ingest_files WHERE owned_formats IS NOT NULL "
                              "AND bucket != 'duplicate'").fetchone()[0],
        "scanned_at": db.get_meta(conn, "ingest_scanned_at"),
        "last_scan": orjson.loads(db.get_meta(conn, "ingest_last_scan", "null")),
        "path": setting(conn, "ingest_path"),
    }


DECISION_FILTERS = {"pending": "f.decision IS NULL", "accepted": "f.decision = 'accepted'",
                    "not_book": "f.decision = 'not_book'", "skipped": "f.decision = 'skipped'"}


def list_files(conn, bucket: str = "all", q: str = "", offset: int = 0, limit: int = 100,
               decision: str | None = None) -> dict:
    where, args = [], []
    if bucket != "all":
        where.append("f.bucket = ?")
        args.append(bucket)
    if decision:
        where.append(DECISION_FILTERS[decision])
    for word in matcher.fold(q).split()[:8]:
        where.append("(lower(f.path) LIKE ? OR lower(coalesce(f.title,'')) LIKE ? OR lower(coalesce(f.authors,'')) LIKE ?)")
        args += [f"%{word}%"] * 3
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM ingest_files f {w}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT f.*, w.title AS work_title, w.first_publish_year AS work_year, dw.title AS decided_title "
        f"FROM ingest_files f LEFT JOIN works w ON w.ol_key = f.work_key "
        f"LEFT JOIN works dw ON dw.ol_key = f.decided_work_key {w} ORDER BY f.path COLLATE NOCASE LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        for k in ("meta_json", "sidecar_json", "candidates_json", "entries_json", "override_json"):
            d[k[:-5]] = orjson.loads(d.pop(k)) if d.get(k) else None
        d["issues"] = [i for i in (d.get("issues") or "").split(",") if i]
        d["owned_formats"] = [x for x in (d.get("owned_formats") or "").split(",") if x]
        d["drm"] = None if d["drm"] is None else bool(d["drm"])
        items.append(d)
    return {"total": total, "offset": offset, "items": items}


def test_folder(path: str, limit: int = 100_000) -> dict:
    root = Path(path)
    if not root.is_dir():
        return {"ok": False, "path": path, "message": f"{path} is not a folder inside the container. Is it mounted?"}
    counts: dict[str, int] = {}
    n = 0
    for _dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            c = extract.classify_ext(f)
            counts[c] = counts.get(c, 0) + 1
            n += 1
            if n >= limit:
                break
        if n >= limit:
            break
    return {"ok": True, "path": path, "files": n, "by_kind": counts, "writable": os.access(root, os.W_OK)}
