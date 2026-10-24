import re
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, ingest, isbn, library, review, search
from .scheduler import Scheduler
from .config import KINDS, get_settings
from .jobs import JobRunner


class ImportRequest(BaseModel):
    kinds: list[str] = list(KINDS)
    require_author: bool = True
    edition_languages: list[str] = []   # lite mode: keep only editions in these MARC codes (empty = all)
    isbn_only: bool = False             # lite mode: keep only editions that have an ISBN

    def job_params(self) -> dict:
        return {"kinds": self.kinds, "require_author": self.require_author,
                "edition_languages": [l.strip().lower() for l in self.edition_languages if l.strip()],
                "isbn_only": self.isbn_only}


class SettingsBody(BaseModel):
    language: str | None = None
    calibre_path: str | None = None
    ingest_path: str | None = None
    ingest_interval_min: float | None = None
    ingest_settle_sec: int | None = None
    library_sync_interval_min: float | None = None


class DecideBody(BaseModel):
    action: str                         # accept | not_book | skip | reset
    work_key: str | None = None
    overrides: dict | None = None       # title, authors, series, series_index, isbn, language


class RematchBody(BaseModel):
    title: str = ""
    authors: list[str] | str = []
    isbn: str | None = None


class BulkBody(BaseModel):
    min_confidence: int | None = None
    author_key: str | None = None
    dry_run: bool = True


class ScanRequest(BaseModel):
    full: bool = False      # re-read every file, not just new/changed ones
    rematch: bool = False   # re-run matching for unchanged files (after a catalog or library update)


class LibraryTest(BaseModel):
    path: str | None = None


class SyncRequest(BaseModel):
    rematch: bool = False


class MatchBody(BaseModel):
    work_key: str | None = None


LANG_RE = re.compile(r"^(all|[a-z]{3})$")
DEFAULT_SETTINGS = {"language": "eng", "calibre_path": library.DEFAULT_CALIBRE_PATH, **ingest.DEFAULTS}
NUMERIC_SETTINGS = {"ingest_interval_min": (0, 1440), "ingest_settle_sec": (0, 86400),
                    "library_sync_interval_min": (0, 10080)}


class DownloadRequest(BaseModel):
    kinds: list[str] = list(KINDS)


def _check_kinds(kinds: list[str]) -> None:
    if not kinds or not set(kinds) <= set(KINDS):
        raise HTTPException(400, "unknown dump kind")


def create_app() -> FastAPI:
    """Build the app. Run with: uvicorn app.main:create_app --factory"""
    settings = get_settings()
    runner = JobRunner(settings)
    scheduler = Scheduler(settings, runner)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = db.connect(settings.db_path)
        db.init(conn)
        conn.close()
        runner.start()
        scheduler.start()
        yield
        scheduler.stop()
        runner.stop()

    app = FastAPI(title="Tomearr", lifespan=lifespan)

    def conn_():
        return db.connect(settings.db_path)

    def job_row(r) -> dict:
        d = dict(r)
        d["progress"] = orjson.loads(d["progress"] or "{}")
        d["params"] = orjson.loads(d["params"] or "{}")
        return d

    @app.get("/api/status")
    def status():
        conn = conn_()
        try:
            dumps = []
            for k in KINDS:
                p = settings.dump_path(k)
                part = p.with_suffix(p.suffix + ".part")
                dumps.append({
                    "kind": k,
                    "present": p.exists(),
                    "size": p.stat().st_size if p.exists() else 0,
                    "partial_size": part.stat().st_size if part.exists() else 0,
                })
            return {
                "counts": {k: int(db.get_meta(conn, f"count_{k}", 0))
                           for k in ("authors", "works", "editions", "isbns", "rated_works")},
                "indexed_at": db.get_meta(conn, "indexed_at"),
                "searchable": search.has_index(conn),
                "schema_version": db.schema_version(conn),
                "editions_lite": orjson.loads(db.get_meta(conn, "editions_lite", "null")),
                "dumps": dumps,
                "dump_dir": str(settings.dump_dir),
            }
        finally:
            conn.close()

    @app.post("/api/catalog/download")
    def catalog_download(req: DownloadRequest):
        _check_kinds(req.kinds)
        return {"job_id": runner.submit("download", {"kinds": req.kinds})}

    @app.post("/api/catalog/import")
    def catalog_import(req: ImportRequest):
        _check_kinds(req.kinds)
        imp = runner.submit("import", req.job_params())
        idx = runner.submit("index", {}, depends_on=imp)
        return {"job_ids": [imp, idx]}

    @app.post("/api/catalog/build")
    def catalog_build(req: ImportRequest):
        """Download, import, then index, as chained jobs."""
        _check_kinds(req.kinds)
        dl = runner.submit("download", {"kinds": req.kinds})
        imp = runner.submit("import", req.job_params(), depends_on=dl)
        idx = runner.submit("index", {}, depends_on=imp)
        return {"job_ids": [dl, imp, idx]}

    @app.post("/api/catalog/reindex")
    def catalog_reindex():
        return {"job_id": runner.submit("index", {})}

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(20, le=100)):
        conn = conn_()
        try:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [job_row(r) for r in rows]
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        runner.cancel(job_id)
        return {"ok": True}

    @app.get("/api/search/authors")
    def api_search_authors(q: str = "", limit: int = Query(25, le=100)):
        conn = conn_()
        try:
            return search.search_authors(conn, q, limit)
        finally:
            conn.close()

    def get_settings_(conn) -> dict:
        out = {k: db.get_meta(conn, f"setting_{k}", v) for k, v in DEFAULT_SETTINGS.items()}
        for k in NUMERIC_SETTINGS:
            out[k] = float(out[k]) if "." in str(out[k]) else int(out[k])
        return out

    @app.get("/api/settings")
    def api_get_settings():
        conn = conn_()
        try:
            return get_settings_(conn)
        finally:
            conn.close()

    @app.put("/api/settings")
    def api_put_settings(body: SettingsBody):
        conn = conn_()
        try:
            if body.language is not None:
                lang = body.language.strip().lower()
                if not LANG_RE.match(lang):
                    raise HTTPException(400, "language must be a 3-letter MARC code (e.g. eng) or 'all'")
                db.set_meta(conn, "setting_language", lang)
            if body.calibre_path is not None:
                p = body.calibre_path.strip().rstrip("/") or "/"
                if not p.startswith("/"):
                    raise HTTPException(400, "Calibre path must be an absolute path inside the container, e.g. /calibre")
                db.set_meta(conn, "setting_calibre_path", p)
            if body.ingest_path is not None:
                p = body.ingest_path.strip().rstrip("/") or "/"
                if not p.startswith("/"):
                    raise HTTPException(400, "Ingest path must be an absolute path inside the container, e.g. /ingest")
                db.set_meta(conn, "setting_ingest_path", p)
            for k, (lo, hi) in NUMERIC_SETTINGS.items():
                v = getattr(body, k)
                if v is not None:
                    if not lo <= v <= hi:
                        raise HTTPException(400, f"{k} must be between {lo} and {hi}")
                    db.set_meta(conn, f"setting_{k}", int(v) if float(v).is_integer() else v)
            return get_settings_(conn)
        finally:
            conn.close()

    @app.get("/api/search/works")
    def api_search_works(q: str = "", limit: int = Query(40, le=100), lang: str | None = None):
        conn = conn_()
        try:
            lang = (lang or get_settings_(conn)["language"]).lower()
            if not LANG_RE.match(lang):
                raise HTTPException(400, "bad language code")
            return library.attach_owned(conn, search.search_works(conn, q, limit, lang=lang))
        finally:
            conn.close()

    @app.get("/api/isbn/{value}")
    def api_isbn(value: str):
        if not isbn.to_isbn13(value):
            raise HTTPException(400, "not a valid ISBN-10 or ISBN-13")
        conn = conn_()
        try:
            r = search.lookup_isbn(conn, value)
            library.attach_owned(conn, r["works"])
            return r
        finally:
            conn.close()

    @app.get("/api/works/{key}")
    def api_work(key: str):
        conn = conn_()
        try:
            w = search.get_work(conn, key)
            if not w:
                raise HTTPException(404, "work not found")
            library.attach_owned(conn, [w])
            w["library_books"] = [dict(r) for r in conn.execute(
                "SELECT calibre_id, title, authors, formats, match_method, match_confidence FROM library_books "
                "WHERE work_key=? ORDER BY calibre_id", (key,))]
            return w
        finally:
            conn.close()

    @app.get("/api/authors/{key}")
    def api_author(key: str):
        conn = conn_()
        try:
            a = search.get_author(conn, key)
            if not a:
                raise HTTPException(404, "author not found")
            library.attach_owned(conn, a["works"])
            return a
        finally:
            conn.close()

    # --- library (read-only mirror of Calibre) ------------------------------
    @app.post("/api/library/test")
    def library_test(body: LibraryTest):
        conn = conn_()
        try:
            path = (body.path or "").strip() or library.calibre_path(conn)
        finally:
            conn.close()
        return library.test_connection(path, settings.config_dir / "cache" / "test")

    @app.post("/api/library/sync")
    def library_sync(body: SyncRequest):
        return {"job_id": runner.submit("library_sync", {"rematch": body.rematch})}

    @app.get("/api/library/stats")
    def library_stats():
        conn = conn_()
        try:
            return library.stats(conn)
        finally:
            conn.close()

    @app.get("/api/library")
    def library_list(q: str = "", filter: str = "all", offset: int = Query(0, ge=0),
                     limit: int = Query(100, ge=1, le=500)):
        if filter not in library.FILTERS:
            raise HTTPException(400, "unknown filter")
        conn = conn_()
        try:
            return library.list_books(conn, q, filter, offset, limit)
        finally:
            conn.close()

    @app.put("/api/library/{calibre_id}/match")
    def library_set_match(calibre_id: int, body: MatchBody):
        conn = conn_()
        try:
            try:
                row = library.set_match(conn, calibre_id, body.work_key)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            if row is None:
                raise HTTPException(404, "book not in library")
            return row
        finally:
            conn.close()

    @app.delete("/api/library/{calibre_id}/match")
    def library_clear_match(calibre_id: int):
        conn = conn_()
        try:
            if not library.clear_manual(conn, calibre_id):
                raise HTTPException(404, "book not in library")
            return {"ok": True}
        finally:
            conn.close()

    # --- ingest (Phase 4: report only, nothing moves) -------------------------
    @app.post("/api/ingest/test")
    def ingest_test(body: LibraryTest):
        conn = conn_()
        try:
            path = (body.path or "").strip() or ingest.setting(conn, "ingest_path")
        finally:
            conn.close()
        return ingest.test_folder(path)

    @app.post("/api/ingest/scan")
    def ingest_scan(body: ScanRequest):
        return {"job_id": runner.submit("ingest_scan", {"full": body.full, "rematch": body.rematch})}

    @app.get("/api/ingest/stats")
    def ingest_stats():
        conn = conn_()
        try:
            return ingest.stats(conn)
        finally:
            conn.close()

    @app.get("/api/ingest")
    def ingest_list(bucket: str = "all", q: str = "", offset: int = Query(0, ge=0),
                    limit: int = Query(100, ge=1, le=500), decision: str | None = None):
        if bucket != "all" and bucket not in ingest.BUCKETS:
            raise HTTPException(400, "unknown bucket")
        if decision is not None and decision not in ingest.DECISION_FILTERS:
            raise HTTPException(400, "unknown decision filter")
        conn = conn_()
        try:
            return ingest.list_files(conn, bucket, q, offset, limit, decision)
        finally:
            conn.close()

    # --- review (Phase 5) ----------------------------------------------------
    def reviewing(fn):
        conn = conn_()
        try:
            return fn(conn)
        except KeyError:
            raise HTTPException(404, "file not found")
        except review.ReviewError as exc:
            raise HTTPException(400, str(exc))
        finally:
            conn.close()

    @app.get("/api/review/queue")
    def review_queue(which: str = "queue"):
        return reviewing(lambda c: review.queue(c, which))

    @app.get("/api/review/{file_id}")
    def review_detail(file_id: int):
        return reviewing(lambda c: review.detail(c, file_id))

    @app.post("/api/review/{file_id}/decide")
    def review_decide(file_id: int, body: DecideBody):
        return reviewing(lambda c: review.decide(c, file_id, body.action, body.work_key, body.overrides))

    @app.post("/api/review/{file_id}/rematch")
    def review_rematch(file_id: int, body: RematchBody):
        return reviewing(lambda c: review.rematch(c, file_id, body.title, body.authors, body.isbn))

    @app.get("/api/review/{file_id}/search")
    def review_search(file_id: int, q: str = ""):
        return reviewing(lambda c: review.search_catalog(c, file_id, q))

    @app.post("/api/review/undo")
    def review_undo():
        r = reviewing(review.undo)
        if r is None:
            raise HTTPException(404, "nothing to undo")
        return r

    @app.post("/api/review/bulk")
    def review_bulk(body: BulkBody):
        return reviewing(lambda c: review.bulk_accept(c, body.min_confidence, body.author_key, body.dry_run))

    # --- frontend ---------------------------------------------------------
    static = settings.static_dir
    if static.exists() and (static / "index.html").exists():
        if (static / "assets").exists():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (static / path).resolve()
            if path and f.is_file() and static.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(static / "index.html")

    return app
