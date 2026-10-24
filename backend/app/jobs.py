"""Single-worker background job runner. Jobs and progress live in SQLite so the UI can poll them."""
import queue
import threading
import time
from pathlib import Path

import httpx
import orjson

from . import covers, db, importer, ingest, library, ol_import
from .config import DUMP_URL, KINDS, Settings


class JobCancelled(Exception):
    pass


def download_dump(url: str, dest: Path, progress, cancelled) -> None:
    """Resumable download to dest (via dest.part)."""
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "tomearr/0.1 (self-hosted ebook catalog)"}
    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60.0) as r:
        if r.status_code == 416:  # already complete
            part.rename(dest)
            return
        if r.status_code == 200 and have:  # server ignored Range; restart
            have = 0
            part.unlink()
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) + have
        mode = "ab" if have else "wb"
        got = have
        last = 0.0
        with open(part, mode) as fh:
            for chunk in r.iter_bytes(1024 * 1024):
                if cancelled():
                    raise JobCancelled()
                fh.write(chunk)
                got += len(chunk)
                now = time.monotonic()
                if now - last > 1:
                    progress({"bytes": got, "total": total})
                    last = now
    progress({"bytes": got, "total": total})
    part.rename(dest)


class JobRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.q: queue.Queue[int] = queue.Queue()
        self.cancel_flags: set[int] = set()
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        conn = db.connect(self.settings.db_path)
        conn.execute("UPDATE jobs SET status='interrupted', finished_at=datetime('now') "
                     "WHERE status IN ('running','queued')")
        conn.commit()
        conn.close()
        self.thread = threading.Thread(target=self._loop, name="job-runner", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.q.put(-1)

    # -- api -----------------------------------------------------------
    def submit(self, kind: str, params: dict | None = None, depends_on: int | None = None) -> int:
        conn = db.connect(self.settings.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO jobs(kind, params, depends_on) VALUES(?,?,?)",
                (kind, orjson.dumps(params or {}).decode(), depends_on),
            )
            conn.commit()
            job_id = cur.lastrowid
        finally:
            conn.close()
        self.q.put(job_id)
        return job_id

    def cancel(self, job_id: int) -> None:
        self.cancel_flags.add(job_id)

    # -- worker --------------------------------------------------------
    def _loop(self) -> None:
        conn = db.connect(self.settings.db_path)
        while not self._stop.is_set():
            job_id = self.q.get()
            if job_id < 0:
                break
            try:
                self._run(conn, job_id)
            except Exception as exc:  # last-resort guard so the worker never dies
                conn.execute("UPDATE jobs SET status='failed', message=?, finished_at=datetime('now') WHERE id=?",
                             (f"{type(exc).__name__}: {exc}", job_id))
                conn.commit()
        conn.close()

    def _run(self, conn, job_id: int) -> None:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job["status"] != "queued":
            return
        if job["depends_on"]:
            dep = conn.execute("SELECT status FROM jobs WHERE id=?", (job["depends_on"],)).fetchone()
            if not dep or dep["status"] != "done":
                conn.execute("UPDATE jobs SET status='skipped', message='A previous step did not finish', "
                             "finished_at=datetime('now') WHERE id=?", (job_id,))
                conn.commit()
                return
        if job_id in self.cancel_flags:
            conn.execute("UPDATE jobs SET status='cancelled', finished_at=datetime('now') WHERE id=?", (job_id,))
            conn.commit()
            return

        conn.execute("UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?", (job_id,))
        conn.commit()
        params = orjson.loads(job["params"])
        last_write = [0.0]

        def progress(p: dict) -> None:
            now = time.monotonic()
            if now - last_write[0] < 0.5 and "step" not in p:
                return
            last_write[0] = now
            conn.execute("UPDATE jobs SET progress=? WHERE id=?", (orjson.dumps(p).decode(), job_id))
            conn.commit()

        def cancelled() -> bool:
            return job_id in self.cancel_flags

        status, message = "done", None
        try:
            self._dispatch(conn, job["kind"], {**params, "_job_id": job_id}, progress, cancelled)
        except (JobCancelled, ol_import.ImportCancelled, library.SyncCancelled, ingest.ScanCancelled,
                importer.ImportCancelled, covers.CoversCancelled):
            status, message = "cancelled", "Cancelled"
        except Exception as exc:
            status, message = "failed", f"{type(exc).__name__}: {exc}"
        conn.execute("UPDATE jobs SET status=?, message=?, finished_at=datetime('now') WHERE id=?",
                     (status, message, job_id))
        conn.commit()
        self.cancel_flags.discard(job_id)

    def _dispatch(self, conn, kind: str, params: dict, progress, cancelled) -> None:
        s = self.settings
        if kind == "download":
            for k in params.get("kinds", list(KINDS)):
                if k not in KINDS:
                    raise ValueError(f"unknown dump kind: {k}")
                progress({"file": k, "step": f"downloading {k}"})
                download_dump(DUMP_URL.format(kind=k), s.dump_path(k),
                              lambda p, k=k: progress({"file": k, **p}), cancelled)
        elif kind == "import":
            kinds = params.get("kinds", list(KINDS))
            # authors first so works can reference them; the rest only need work keys
            for k in KINDS:
                if k not in kinds:
                    continue
                path = s.dump_path(k)
                if not path.exists():
                    raise FileNotFoundError(f"{path.name} not found in the dumps folder")
                if k == "authors":
                    ol_import.import_authors(conn, path, progress, cancelled)
                elif k == "works":
                    ol_import.import_works(conn, path, progress, cancelled,
                                           require_author=params.get("require_author", True))
                elif k == "editions":
                    ol_import.import_editions(conn, path, progress, cancelled,
                                              languages=params.get("edition_languages") or None,
                                              isbn_only=params.get("isbn_only", False))
                else:
                    ol_import.import_signals(conn, path, k, progress, cancelled)
        elif kind == "index":
            ol_import.build_indexes(conn, progress)
        elif kind == "ingest_scan":
            ingest.scan(conn, s, progress, cancelled, full=params.get("full", False),
                        rematch=params.get("rematch", False))
        elif kind == "calibre_import":
            res = importer.run(conn, s, progress, cancelled, keys=params.get("keys"), batch=params.get("_job_id"),
                               library=params.get("library", False))
            if res["done"]:
                # refresh what Tomearr knows: Calibre's new books, and the ingest folder without the imported files
                self.submit("library_sync", {"after_import": True})
                self.submit("ingest_scan", {"after_import": True})
        elif kind == "fix_covers":
            res = covers.fix_missing(conn, s, importer.target(conn), progress, cancelled, batch=params.get("_job_id"))
            if res["fixed"]:
                self.submit("library_sync", {"after_covers": True})
        elif kind == "library_sync":
            library.sync(conn, s.config_dir / "cache", progress, cancelled, rematch=params.get("rematch", False))
        else:
            raise ValueError(f"unknown job kind: {kind}")
