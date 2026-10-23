from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, search
from .config import KINDS, get_settings
from .jobs import JobRunner


class ImportRequest(BaseModel):
    kinds: list[str] = list(KINDS)
    require_author: bool = True


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
                "counts": {
                    "authors": int(db.get_meta(conn, "count_authors", 0)),
                    "works": int(db.get_meta(conn, "count_works", 0)),
                },
                "indexed_at": db.get_meta(conn, "indexed_at"),
                "searchable": search.has_index(conn),
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
        imp = runner.submit("import", {"kinds": req.kinds, "require_author": req.require_author})
        idx = runner.submit("index", {}, depends_on=imp)
        return {"job_ids": [imp, idx]}

    @app.post("/api/catalog/build")
    def catalog_build(req: ImportRequest):
        """Download, import, then index, as chained jobs."""
        _check_kinds(req.kinds)
        dl = runner.submit("download", {"kinds": req.kinds})
        imp = runner.submit("import", {"kinds": req.kinds, "require_author": req.require_author}, depends_on=dl)
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

    @app.get("/api/search/works")
    def api_search_works(q: str = "", limit: int = Query(40, le=100)):
        conn = conn_()
        try:
            return search.search_works(conn, q, limit)
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
