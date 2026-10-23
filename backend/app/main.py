import re
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, isbn, search
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


LANG_RE = re.compile(r"^(all|[a-z]{3})$")
DEFAULT_SETTINGS = {"language": "eng"}


class DownloadRequest(BaseModel):
    kinds: list[str] = list(KINDS)


def _check_kinds(kinds: list[str]) -> None:
    if not kinds or not set(kinds) <= set(KINDS):
        raise HTTPException(400, "unknown dump kind")


def create_app() -> FastAPI:
    """Build the app. Run with: uvicorn app.main:create_app --factory"""
    settings = get_settings()
    runner = JobRunner(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = db.connect(settings.db_path)
        db.init(conn)
        conn.close()
        runner.start()
        yield
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
        return {k: db.get_meta(conn, f"setting_{k}", v) for k, v in DEFAULT_SETTINGS.items()}

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
            return search.search_works(conn, q, limit, lang=lang)
        finally:
            conn.close()

    @app.get("/api/isbn/{value}")
    def api_isbn(value: str):
        if not isbn.to_isbn13(value):
            raise HTTPException(400, "not a valid ISBN-10 or ISBN-13")
        conn = conn_()
        try:
            return search.lookup_isbn(conn, value)
        finally:
            conn.close()

    @app.get("/api/works/{key}")
    def api_work(key: str):
        conn = conn_()
        try:
            w = search.get_work(conn, key)
            if not w:
                raise HTTPException(404, "work not found")
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
            return a
        finally:
            conn.close()

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
