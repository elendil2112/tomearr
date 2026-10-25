"""Phase 6: turn approved files into EPUBs in Calibre, safely.

Order of operations for every file, so nothing is ever lost:
  1. copy the file to /config/staging (the ingest file itself is never modified)
  2. convert to EPUB if needed, and write the metadata into the staged EPUB
  3. add it to Calibre through calibredb (new book, or an EPUB format on the book you already own)
  4. ask Calibre to confirm the EPUB is there
  5. back up any formats being replaced (MOBI, AZW3, ...) into the trash, then remove them from Calibre
  6. move the original ingest file into /config/trash (kept for `trash_days`, then purged)
Each step is logged in `imports` with what's needed to undo it, and a failure part-way rolls back what was done.
"""
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import orjson

from . import calibre_cli, db, extract, ingest, leftovers, library, review
from .calibre_cli import Calibre, CalibreCLIError, Target

DEFAULTS = {
    "calibre_mode": "direct",                 # direct (no login needed) | server
    "calibre_url": "",
    "calibre_library_id": "",
    "calibre_username": "",
    "trash_days": "14",
    "replace_formats": "MOBI,AZW,AZW3,PRC",   # removed from Calibre once the book has an EPUB
    "delete_duplicates": "1",                 # trash ingest files that are byte-identical to a Calibre file
    "import_auto": "0",                       # import ready files automatically after each scan
    "fetch_covers": "1",                      # give coverless books the Open Library cover
}
SECRET = "calibre_password"
NO_CONVERT = {"EPUB"}
# Imports that deleted something; with the trash off there's nothing to put back, so they can't be undone.
NO_UNDO_WITHOUT_TRASH = {"add", "add_format", "convert_existing", "delete_duplicate", "already_have", "unpack",
                         "replace", "cleanup"}
# After these, the original has left the ingest folder, so its folder may now hold only leftovers.
CLEANS_UP = {"add", "add_format", "replace", "already_have", "delete_duplicate", "unpack"}


class ImportCancelled(Exception):
    pass


class ImportError_(RuntimeError):
    pass


def setting(conn, key: str) -> str:
    return db.get_meta(conn, f"setting_{key}", DEFAULTS.get(key, ""))


def target(conn) -> Target:
    return Target(mode=setting(conn, "calibre_mode"), url=setting(conn, "calibre_url"),
                  library_id=setting(conn, "calibre_library_id"), username=setting(conn, "calibre_username"),
                  password=db.get_meta(conn, f"setting_{SECRET}", "") or "",
                  library_path=library.calibre_path(conn))


def replace_formats(conn) -> set[str]:
    return {f.strip().upper() for f in setting(conn, "replace_formats").split(",") if f.strip()} - {"EPUB"}


def _calibre_root(conn) -> Path:
    return Path(library.calibre_path(conn))


def _ingest_root(conn) -> Path:
    return Path(ingest.setting(conn, "ingest_path"))


def _config_dir(settings) -> Path:
    return Path(settings.config_dir)


# --- metadata --------------------------------------------------------------------------------------------------

def build_meta(conn, file_row, work_key: str) -> dict:
    """What gets written into the EPUB and Calibre: your edits win, then the catalog's canonical title and
    authors, then what the file said (ISBN, series, language)."""
    rd = review.reading(file_row)
    over = orjson.loads(file_row["override_json"]) if file_row["override_json"] else {}
    w = conn.execute("SELECT title, subtitle, first_publish_year FROM works WHERE ol_key=?", (work_key,)).fetchone()
    names = [r[0] for r in conn.execute(
        "SELECT a.name FROM work_authors wa JOIN authors a ON a.ol_key = wa.author_key WHERE wa.work_key=? "
        "ORDER BY a.work_count DESC", (work_key,))]
    title = over.get("title") or (w["title"] if w else None) or rd.get("title")
    authors = over.get("authors") or names or rd.get("authors") or []
    isbns = [over["isbn"]] if over.get("isbn") else (rd.get("isbns") or [])
    lang = over.get("language") or rd.get("language")
    year = rd.get("year") or (w["first_publish_year"] if w else None)
    meta = {
        "title": title, "authors": authors, "isbn": isbns[0] if isbns else None,
        "identifiers": {"openlibrary": work_key},
        "series": over.get("series") or rd.get("series"),
        "series_index": over.get("series_index", rd.get("series_index")),
        "languages": [lang] if lang else [],
        "pubdate": f"{int(year):04d}-01-01" if year else None,
    }
    return meta


# --- the plan (dry run) ----------------------------------------------------------------------------------------

def _owned(conn, work_key: str) -> list[dict]:
    return [{"calibre_id": r[0], "formats": [f for f in (r[1] or "").split(",") if f]}
            for r in conn.execute("SELECT calibre_id, formats FROM library_books WHERE work_key=? ORDER BY calibre_id",
                                  (work_key,))]


def plan(conn) -> dict:
    """Everything an import would do, without doing any of it."""
    items = []
    rf = replace_formats(conn)
    rows = conn.execute(
        f"SELECT f.*, w.title AS work_title FROM ingest_files f "
        f"LEFT JOIN works w ON w.ol_key = coalesce(f.decided_work_key, f.work_key) "
        f"WHERE {ingest.READY_SQL} ORDER BY f.path COLLATE NOCASE").fetchall()
    for r in rows:
        wk = r["decided_work_key"] or r["work_key"]
        it = {"key": f"file:{r['id']}", "file_id": r["id"], "path": r["path"], "format": r["format"],
              "work_key": wk, "work_title": r["work_title"], "select": True}
        if r["format"] == "PDF" and r["pdf_state"] != "approved":
            state = r["pdf_state"]
            if state == "convert":
                it.update(kind="pdf_convert", steps=["convert the PDF to EPUB for you to check (nothing is imported yet)"])
            elif state == "converted":
                it.update(kind="pdf_waiting", select=False, steps=["converted EPUB is waiting for your approval"])
            else:
                it.update(kind="pdf_hold", select=False,
                          steps=["PDF: opt in to convert it first (PDF to EPUB is often rough)"])
            items.append(it)
            continue
        owned = _owned(conn, wk)
        convert = r["format"] not in NO_CONVERT
        steps = [f"convert {r['format']} to EPUB"] if convert and r["format"] != "PDF" else []
        if r["format"] == "PDF":
            steps = ["use the EPUB you approved"]
        if any("EPUB" in o["formats"] for o in owned):
            have = next(o for o in owned if "EPUB" in o["formats"])
            choice = r["compare_choice"]
            if choice == "ingest":
                it.update(kind="replace", select=True, calibre_id=have["calibre_id"], choice=choice,
                          steps=steps + [f"replace the EPUB in Calibre (#{have['calibre_id']}) with this one; the old "
                                         "EPUB goes to the trash", "move the original to the trash"])
            else:
                it.update(kind="already_have", select=choice == "calibre", calibre_id=have["calibre_id"], choice=choice,
                          steps=[f"Calibre already has this book as EPUB (#{have['calibre_id']})"
                                 + ("; you chose to keep Calibre's, so move this file to the trash" if choice == "calibre"
                                    else "; you'll decide later" if choice == "skip"
                                    else "; compare them to choose, or move this file to the trash")])
        elif owned:
            o = owned[0]
            drop = sorted(set(o["formats"]) & rf)
            it.update(kind="add_format", calibre_id=o["calibre_id"], remove=drop,
                      steps=steps + [f"add the EPUB to your existing book #{o['calibre_id']}"] +
                            ([f"remove {', '.join(drop)} from it (backed up first)"] if drop else []) +
                            ["move the original to the trash"])
        else:
            it.update(kind="add", steps=steps + ["write metadata and add a new book to Calibre",
                                                 "move the original to the trash"])
        items.append(it)

    if setting(conn, "delete_duplicates") == "1":
        for r in conn.execute("SELECT * FROM ingest_files WHERE bucket='duplicate' AND duplicate_of LIKE 'calibre:%' "
                              "AND (decision IS NULL OR decision != 'not_book') ORDER BY path"):
            items.append({"key": f"file:{r['id']}", "file_id": r["id"], "path": r["path"], "format": r["format"],
                          "kind": "delete_duplicate", "select": True, "calibre_id": int(r["duplicate_of"][8:]),
                          "steps": [f"identical to a file already in Calibre (#{r['duplicate_of'][8:]}); move to the trash"]})
    for r in conn.execute("SELECT * FROM ingest_files WHERE bucket='archive' AND (decision IS NULL OR "
                          "decision != 'not_book') ORDER BY path"):
        entries = orjson.loads(r["entries_json"]) if r["entries_json"] else []
        items.append({"key": f"file:{r['id']}", "file_id": r["id"], "path": r["path"], "format": r["format"],
                      "kind": "unpack", "select": True,
                      "steps": [f"unpack{f' ({len(entries)} ebook files)' if entries else ''} into a folder next to it; "
                                "the next scan picks them up", "move the archive to the trash"]})
    root = _ingest_root(conn)
    if root.is_dir():
        settle = int(ingest.setting(conn, "ingest_settle_sec"))
        for folder, files in leftovers.sweep_candidates(root, settle):
            rel = folder.relative_to(root.resolve()).as_posix()
            kinds = sorted({(f.suffix.lower().lstrip(".") or f.name) for f in files})
            items.append({"key": f"dir:{rel}", "path": rel + "/", "kind": "cleanup", "select": True,
                          "files": len(files),
                          "steps": [f"only leftovers here ({len(files)} file{'s' if len(files) != 1 else ''}"
                                    f"{': ' + ', '.join(kinds[:6]) if kinds else ', empty'}); remove the folder"]})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {"items": items, "counts": counts, "selected": sum(1 for i in items if i["select"]),
            "target": target(conn).describe(), "calibre_tools": calibre_cli.version()}


def plan_library(conn) -> dict:
    """Books already in Calibre that only have formats you want replaced (MOBI, AZW3, ...): convert to EPUB."""
    rf = replace_formats(conn)
    priority = ["AZW3", "MOBI", "AZW", "PRC"]
    items = []
    for r in conn.execute("SELECT calibre_id, title, authors, formats FROM library_books "
                          "WHERE formats IS NOT NULL AND instr(formats, ',EPUB,') = 0 ORDER BY author_sort, title"):
        fmts = [f for f in r["formats"].split(",") if f]
        src = next((f for f in priority if f in fmts and f in rf), None) or next((f for f in fmts if f in rf), None)
        if not src:
            continue
        drop = sorted(set(fmts) & rf)
        items.append({"key": f"book:{r['calibre_id']}", "kind": "convert_existing", "calibre_id": r["calibre_id"],
                      "title": r["title"], "authors": r["authors"], "format": src, "remove": drop, "select": True,
                      "steps": [f"convert {src} to EPUB and add it to the book",
                                f"remove {', '.join(drop)} (backed up to the trash first)"]})
    return {"items": items, "counts": {"convert_existing": len(items)}, "selected": len(items),
            "target": target(conn).describe(), "calibre_tools": calibre_cli.version()}


# --- doing it --------------------------------------------------------------------------------------------------

def _trash_dir(settings, batch) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _config_dir(settings) / "trash" / f"{stamp}-job{batch}"


def _move_to_trash(src: Path, trash: Path | None, rel: str) -> Path | None:
    """Move an original into the trash, or delete it outright when the trash is off (trash_days = 0)."""
    if trash is None:
        src.unlink()
        return None
    dst = trash / "ingest" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}.{int(time.time())}{dst.suffix}")
    shutil.move(str(src), str(dst))
    return dst


def _backup_calibre_format(conn, calibre_id: int, fmt: str, trash: Path) -> Path | None:
    """Copy a Calibre format file (read from the /calibre mount) into the trash before it's removed."""
    r = conn.execute("SELECT relpath FROM calibre_files WHERE calibre_id=? AND format=?", (calibre_id, fmt)).fetchone()
    if not r:
        return None
    src = _calibre_root(conn) / r["relpath"]
    if not src.is_file():
        return None
    dst = trash / "calibre" / str(calibre_id) / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _log(conn, **kw) -> int:
    cols = ("batch", "action", "path", "sha256", "source_format", "work_key", "calibre_id", "added_format",
            "removed_json", "trash_path", "meta_json", "status", "error")
    cur = conn.execute(f"INSERT INTO imports({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       tuple(kw.get(c) for c in cols))
    conn.commit()
    return cur.lastrowid


def _replace_old_formats(conn, cal: Calibre, cid: int, drop: list[str], trash: Path | None) -> tuple[list, list]:
    removed, kept = [], []
    for f in drop:
        if trash is None:           # trash off: remove without keeping a copy (as asked)
            cal.remove_format(cid, f)
            removed.append({"format": f, "backup": None})
            continue
        backup = _backup_calibre_format(conn, cid, f, trash)
        if backup is None:
            kept.append(f)          # never remove what we couldn't back up
            continue
        cal.remove_format(cid, f)
        removed.append({"format": f, "backup": str(backup)})
    return removed, kept


def _import_file(conn, settings, cal: Calibre, it: dict, batch: int, trash: Path) -> dict:
    r = conn.execute("SELECT * FROM ingest_files WHERE id=?", (it["file_id"],)).fetchone()
    root = _ingest_root(conn)
    src = root / r["path"]
    if not src.is_file():
        raise ImportError_("the file is no longer in the ingest folder")
    if r["sha256"] and extract.sha256_file(src) != r["sha256"]:
        raise ImportError_("the file changed since it was scanned; rescan first")
    stage = _config_dir(settings) / "staging" / str(r["id"])
    epub = stage / "book.epub"
    kind = it["kind"]

    if kind == "delete_duplicate":
        t = _move_to_trash(src, trash, r["path"])
        conn.execute("DELETE FROM ingest_files WHERE id=?", (r["id"],))
        return {"action": "delete_duplicate", "path": r["path"], "sha256": r["sha256"], "trash_path": str(t) if t else None,
                "calibre_id": it.get("calibre_id")}

    if kind == "unpack":
        dest = src.with_name(src.name.rsplit(".", 1)[0] + " (unpacked)")
        if dest.exists():
            raise ImportError_(f"{dest.name} already exists")
        files = calibre_cli.unpack(src, dest)
        old = time.time() - 3600   # extracted files count as settled, so the next scan reads them
        for f in files:
            os.utime(f, (old, old))
        t = _move_to_trash(src, trash, r["path"])
        conn.execute("DELETE FROM ingest_files WHERE id=?", (r["id"],))
        return {"action": "unpack", "path": r["path"], "sha256": r["sha256"], "trash_path": str(t) if t else None,
                "meta_json": orjson.dumps({"extracted": [str(f.relative_to(root)) for f in files]}).decode()}

    if kind == "already_have":
        t = _move_to_trash(src, trash, r["path"])
        conn.execute("DELETE FROM ingest_files WHERE id=?", (r["id"],))
        return {"action": "already_have", "path": r["path"], "sha256": r["sha256"], "trash_path": str(t) if t else None,
                "calibre_id": it.get("calibre_id")}

    if kind == "pdf_convert":
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        calibre_cli.convert(src, epub)
        conn.execute("UPDATE ingest_files SET pdf_state='converted' WHERE id=?", (r["id"],))
        return {"action": "pdf_convert", "path": r["path"], "sha256": r["sha256"]}

    # add / add_format
    wk = it["work_key"]
    meta = build_meta(conn, r, wk)
    if r["format"] == "PDF":
        if not epub.is_file():
            raise ImportError_("the approved EPUB is missing from staging; convert the PDF again")
    else:
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        if r["format"] in NO_CONVERT:
            shutil.copy2(src, epub)
        else:
            calibre_cli.convert(src, epub)
    if setting(conn, "fetch_covers") == "1" and not calibre_cli.has_cover(epub):
        from .covers import Fetcher
        img = Fetcher(_config_dir(settings) / "cache" / "covers").for_work(conn, wk, meta.get("isbn"))
        if img:
            meta = {**meta, "cover": str(img)}
    calibre_cli.write_meta(epub, meta)

    if kind == "replace":
        cid = it["calibre_id"]
        before = cal.formats(cid)
        if "EPUB" not in before:
            raise ImportError_(f"book #{cid} no longer has an EPUB to replace; sync the library")
        backup = _backup_calibre_format(conn, cid, "EPUB", trash) if trash is not None else None
        if trash is not None and backup is None:
            raise ImportError_("couldn't back up Calibre's current EPUB, so it wasn't replaced")
        cal.add_format(cid, epub)            # replaces the existing EPUB
        if "EPUB" not in cal.formats(cid):
            if backup:
                cal.add_format(cid, backup)
            raise ImportError_("Calibre did not confirm the new EPUB")
        # A coverless Calibre record takes the new EPUB's cover (Calibre keeps the record's cover separately).
        cover_set = False
        lb = conn.execute("SELECT has_cover FROM library_books WHERE calibre_id=?", (cid,)).fetchone()
        if lb is not None and not lb["has_cover"]:
            img = stage / "cover-from-epub.jpg"
            try:
                calibre_cli._run([calibre_cli.tool("ebook-meta"), str(epub), "--get-cover", str(img)], 120)
                if img.is_file() and img.stat().st_size:
                    cal.set_fields(cid, {"cover": str(img)})
                    conn.execute("UPDATE library_books SET has_cover=1 WHERE calibre_id=?", (cid,))
                    cover_set = True
            except CalibreCLIError:
                pass
        t = _move_to_trash(src, trash, r["path"])
        conn.execute("DELETE FROM ingest_files WHERE id=?", (r["id"],))
        shutil.rmtree(stage, ignore_errors=True)
        return {"action": "replace", "path": r["path"], "sha256": r["sha256"], "source_format": r["format"],
                "work_key": wk, "calibre_id": cid, "added_format": None,
                "removed_json": orjson.dumps([{"format": "EPUB", "backup": str(backup) if backup else None}]).decode(),
                "trash_path": str(t) if t else None,
                "meta_json": orjson.dumps({"cover_added": "cover" in meta, "calibre_cover_set": cover_set}).decode()}

    removed, kept, cid, added_new = [], [], None, False
    try:
        if kind == "add":
            cid = cal.add(epub, meta)
            added_new = True
        else:
            cid = it["calibre_id"]
            before = cal.formats(cid)
            if not before and cal.book(cid) is None:
                raise ImportError_(f"book #{cid} is no longer in Calibre; sync the library and try again")
            if "EPUB" in before:
                raise ImportError_(f"book #{cid} already has an EPUB in Calibre")
            cal.add_format(cid, epub)
        if "EPUB" not in cal.formats(cid):
            raise ImportError_("Calibre did not confirm the EPUB was added")
        if kind == "add_format":
            removed, kept = _replace_old_formats(conn, cal, cid, it.get("remove", []), trash)
    except Exception:
        # roll back whatever reached Calibre so a failed import leaves no half-added book
        try:
            if added_new and cid:
                cal.remove(cid)
            elif cid and kind == "add_format" and "EPUB" in cal.formats(cid):
                cal.remove_format(cid, "EPUB")
        except Exception:  # noqa: BLE001
            pass
        raise

    t = _move_to_trash(src, trash, r["path"])
    conn.execute("DELETE FROM ingest_files WHERE id=?", (r["id"],))
    shutil.rmtree(stage, ignore_errors=True)
    return {"action": kind, "path": r["path"], "sha256": r["sha256"], "source_format": r["format"], "work_key": wk,
            "calibre_id": cid, "added_format": "EPUB", "removed_json": orjson.dumps(removed).decode(),
            "trash_path": str(t) if t else None,
            "meta_json": orjson.dumps({**{k: v for k, v in meta.items() if k != "cover"}, "cover_added": "cover" in meta,
                                       "kept_formats": kept}).decode()}


def _cleanup_folder(conn, it: dict, trash: Path | None) -> dict:
    root = _ingest_root(conn).resolve()
    folder = (root / it["path"].rstrip("/")).resolve()
    if folder == root or root not in folder.parents or not folder.is_dir():
        raise ImportError_("that folder is gone or outside the ingest folder")
    files = leftovers.junk_tree(folder, int(ingest.setting(conn, "ingest_settle_sec")))
    if files is None:
        raise ImportError_("something other than leftovers is in that folder now; left it alone")
    gone = leftovers._remove_tree(root, folder, files, trash)
    return {"action": "cleanup", "path": it["path"], "meta_json": orjson.dumps({"leftovers": gone}).decode()}


def _convert_existing(conn, settings, cal: Calibre, it: dict, trash: Path) -> dict:
    cid, fmt = it["calibre_id"], it["format"]
    r = conn.execute("SELECT relpath FROM calibre_files WHERE calibre_id=? AND format=?", (cid, fmt)).fetchone()
    if not r:
        raise ImportError_(f"no {fmt} file recorded for book #{cid}; sync the library first")
    src = _calibre_root(conn) / r["relpath"]
    if not src.is_file():
        raise ImportError_(f"can't read {src} (is the Calibre library mounted at {_calibre_root(conn)}?)")
    stage = _config_dir(settings) / "staging" / f"book-{cid}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    epub = stage / "book.epub"
    calibre_cli.convert(src, epub)
    before = cal.formats(cid)
    if "EPUB" in before:
        raise ImportError_(f"book #{cid} already has an EPUB")
    if not before:
        raise ImportError_(f"book #{cid} is not in Calibre any more")
    cal.add_format(cid, epub)
    try:
        if "EPUB" not in cal.formats(cid):
            raise ImportError_("Calibre did not confirm the EPUB was added")
        removed, kept = _replace_old_formats(conn, cal, cid, [f for f in it.get("remove", []) if f in before], trash)
    except Exception:
        try:
            cal.remove_format(cid, "EPUB")
        except Exception:  # noqa: BLE001
            pass
        raise
    shutil.rmtree(stage, ignore_errors=True)
    return {"action": "convert_existing", "source_format": fmt, "calibre_id": cid, "added_format": "EPUB",
            "removed_json": orjson.dumps(removed).decode(), "meta_json": orjson.dumps({"kept_formats": kept}).decode()}


def run(conn, settings, progress, cancelled, keys: list[str] | None = None, batch: int | None = None,
        library: bool = False) -> dict:
    """Do the selected plan items (all default-selected ones if keys is None)."""
    p = plan_library(conn) if library else plan(conn)
    items = [i for i in p["items"] if (i["key"] in keys if keys is not None else i["select"])]
    items = [i for i in items if i["kind"] not in ("pdf_hold", "pdf_waiting")]
    needs_calibre = any(i["kind"] in ("add", "add_format", "convert_existing", "replace") for i in items)
    needs_ingest_write = any("file_id" in i or i["kind"] == "cleanup" for i in items)
    if needs_ingest_write and not os.access(_ingest_root(conn), os.W_OK):
        raise ImportError_(f"The ingest folder {_ingest_root(conn)} is read-only. Remove ':ro' from the /ingest "
                           "mount in docker-compose.yml so Tomearr can move imported files out of it.")
    cal = Calibre(target(conn))
    if needs_calibre:
        progress({"step": "checking the Calibre connection"})
        cal.test()
    # trash_days = 0 means no trash: originals and replaced formats are deleted as soon as Calibre confirms
    trash = None if int(setting(conn, "trash_days") or 0) == 0 else _trash_dir(settings, batch or 0)
    done = failed = 0
    added: dict[str, int] = {}     # work -> Calibre book added earlier in this run
    for n, it in enumerate(items):
        if cancelled():
            raise ImportCancelled()
        if it["kind"] in ("add", "add_format") and it.get("work_key") in added:
            # Another file of a book this run already added as EPUB: Calibre keeps one EPUB per book.
            cid = added[it["work_key"]]
            it = {**it, "kind": "already_have", "calibre_id": cid}
        progress({"step": "importing", "done": n, "total": len(items), "current": it.get("path") or it.get("title"),
                  "failed": failed})
        try:
            if it["kind"] == "convert_existing":
                res = _convert_existing(conn, settings, cal, it, trash)
            elif it["kind"] == "cleanup":
                res = _cleanup_folder(conn, it, trash)
            else:
                res = _import_file(conn, settings, cal, it, batch or 0, trash)
            if res.get("action") in CLEANS_UP and res.get("path"):
                gone = leftovers.cleanup_after(_ingest_root(conn), res["path"], trash,
                                               int(ingest.setting(conn, "ingest_settle_sec")))
                if gone:
                    meta = orjson.loads(res.get("meta_json") or "{}") or {}
                    res["meta_json"] = orjson.dumps({**meta, "leftovers": gone}).decode()
            if trash is None and res.get("action") in NO_UNDO_WITHOUT_TRASH:
                meta = orjson.loads(res.get("meta_json") or "{}") or {}
                res["meta_json"] = orjson.dumps({**meta, "no_undo": True}).decode()
            _log(conn, batch=batch, status="done", **res)
            if res.get("action") in ("add", "add_format") and res.get("work_key"):
                added[res["work_key"]] = res["calibre_id"]
            done += 1
        except (CalibreCLIError, ImportError_, OSError, zipfile.BadZipFile) as exc:
            conn.rollback()
            _log(conn, batch=batch, action=it["kind"], path=it.get("path"), calibre_id=it.get("calibre_id"),
                 work_key=it.get("work_key"), source_format=it.get("format"), status="failed", error=str(exc)[:500])
            failed += 1
        conn.commit()
    result = {"step": "done", "done": done, "failed": failed, "total": len(items)}
    progress(result)
    return result


# --- undo -------------------------------------------------------------------------------------------------------

def _restore(conn, trash_path: str | None, rel: str | None) -> str | None:
    if not trash_path or not rel or not Path(trash_path).exists():
        return None
    dst = _ingest_root(conn) / rel
    if dst.exists():
        dst = dst.with_name(f"{dst.stem} (restored){dst.suffix}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(trash_path, str(dst))
    old = time.time() - 3600
    os.utime(dst, (old, old))
    return str(dst)


def undo(conn, import_id: int, settings=None) -> dict:
    r = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if not r:
        raise KeyError(import_id)
    if r["status"] != "done":
        raise ImportError_(f"this import is {r['status']}, nothing to undo")
    if r["action"] == "set_cover":
        raise ImportError_("Calibre can't remove a cover from the command line; change it in Calibre or Calibre-Web")
    if (orjson.loads(r["meta_json"] or "{}") or {}).get("no_undo"):
        raise ImportError_("the original was deleted right away (trash set to 0 days), so this import can't be undone")
    cal = Calibre(target(conn))
    a = r["action"]
    if a == "add":
        cal.remove(r["calibre_id"])
    elif a == "replace":
        rm = orjson.loads(r["removed_json"] or "[]")
        if not rm or not rm[0].get("backup") or not Path(rm[0]["backup"]).exists():
            raise ImportError_("the previous EPUB's backup is gone (trash purged?), so it can't be put back")
        cal.add_format(r["calibre_id"], Path(rm[0]["backup"]))
    elif a in ("add_format", "convert_existing"):
        for rm in orjson.loads(r["removed_json"] or "[]"):
            if not Path(rm["backup"]).exists():
                raise ImportError_(f"the backup of {rm['format']} is gone (trash purged?)")
            cal.add_format(r["calibre_id"], Path(rm["backup"]))
        if r["added_format"] and r["added_format"] in cal.formats(r["calibre_id"]):
            cal.remove_format(r["calibre_id"], r["added_format"])
    elif a == "pdf_convert":
        conn.execute("UPDATE ingest_files SET pdf_state=NULL WHERE path=?", (r["path"],))
        fid = conn.execute("SELECT id FROM ingest_files WHERE path=?", (r["path"],)).fetchone()
        if fid and settings is not None:
            shutil.rmtree(_config_dir(settings) / "staging" / str(fid[0]), ignore_errors=True)
    elif a == "unpack":
        root = _ingest_root(conn)
        for rel in (orjson.loads(r["meta_json"] or "{}").get("extracted") or []):
            (root / rel).unlink(missing_ok=True)
        folder = (root / r["path"]).with_name(Path(r["path"]).name.rsplit(".", 1)[0] + " (unpacked)")
        if folder.is_dir():
            for d in sorted(folder.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            if not any(folder.iterdir()):
                folder.rmdir()
    restored = _restore(conn, r["trash_path"], r["path"]) if a != "cleanup" else None
    left = (orjson.loads(r["meta_json"] or "{}") or {}).get("leftovers") or []
    if left:
        leftovers.restore(left, _ingest_root(conn))
    conn.execute("UPDATE imports SET status='undone', undone_at=datetime('now') WHERE id=?", (import_id,))
    conn.commit()
    return {"id": import_id, "action": a, "restored": restored}


def undo_batch(conn, batch: int, settings=None) -> dict:
    ids = [r[0] for r in conn.execute("SELECT id FROM imports WHERE batch=? AND status='done' AND action != 'set_cover' "
                                      "AND coalesce(json_extract(meta_json, '$.no_undo'), 0) = 0 "
                                      "ORDER BY id DESC", (batch,))]
    out, errors = [], []
    for i in ids:
        try:
            out.append(undo(conn, i, settings))
        except (CalibreCLIError, ImportError_, OSError) as exc:
            errors.append({"id": i, "error": str(exc)})
    return {"undone": len(out), "errors": errors}


def history(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT i.*, w.title AS work_title FROM imports i LEFT JOIN works w ON w.ol_key = i.work_key "
                        "ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["removed"] = orjson.loads(d.pop("removed_json") or "[]")
        d["meta"] = orjson.loads(d.pop("meta_json") or "null")
        out.append(d)
    return out


# --- PDFs ---------------------------------------------------------------------------------------------------------

def set_compare_choice(conn, file_id: int, choice: str | None) -> dict:
    if choice not in ("calibre", "ingest", "skip", None):
        raise ImportError_("choice must be calibre, ingest or skip")
    cur = conn.execute("UPDATE ingest_files SET compare_choice=? WHERE id=?", (choice, file_id))
    if not cur.rowcount:
        raise KeyError(file_id)
    conn.commit()
    return {"id": file_id, "choice": choice}


def pdf_action(conn, settings, file_id: int, action: str) -> dict:
    r = conn.execute("SELECT * FROM ingest_files WHERE id=?", (file_id,)).fetchone()
    if not r:
        raise KeyError(file_id)
    if r["format"] != "PDF":
        raise ImportError_("not a PDF")
    stage = _config_dir(settings) / "staging" / str(file_id)
    if action == "convert":
        state = "convert"
    elif action == "approve":
        if r["pdf_state"] != "converted" or not (stage / "book.epub").is_file():
            raise ImportError_("convert it first, then check the result")
        state = "approved"
    elif action == "reject":
        shutil.rmtree(stage, ignore_errors=True)
        state = None
    else:
        raise ImportError_(f"unknown action {action}")
    conn.execute("UPDATE ingest_files SET pdf_state=? WHERE id=?", (state, file_id))
    conn.commit()
    return {"id": file_id, "pdf_state": state}


def pdf_preview(settings, file_id: int) -> dict:
    """Size and a text sample of the staged EPUB, so you can judge the conversion."""
    epub = _config_dir(settings) / "staging" / str(file_id) / "book.epub"
    if not epub.is_file():
        raise KeyError(file_id)
    sample, chapters = "", 0
    with zipfile.ZipFile(epub) as z:
        pages = sorted(n for n in z.namelist() if n.endswith((".html", ".xhtml", ".htm")))
        chapters = len(pages)
        import re as _re
        for n in pages:
            html = z.read(n)[:200_000].decode("utf-8", "replace")
            html = _re.sub(r"(?is)<(head|style|script)\b.*?</\1>", " ", html)
            text = _re.sub(r"<[^>]+>", " ", html)
            text = _re.sub(r"\s+", " ", text).strip()
            if text:
                sample += text + "\n\n"
            if len(sample) > 3000:
                break
    return {"id": file_id, "size": epub.stat().st_size, "files": chapters, "sample": sample[:3000], "path": str(epub)}


# --- trash --------------------------------------------------------------------------------------------------------

def purge_trash(conn, settings) -> int:
    days = int(setting(conn, "trash_days") or 0)
    root = _config_dir(settings) / "trash"
    if not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
