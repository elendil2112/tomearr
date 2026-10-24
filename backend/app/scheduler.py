"""Interval jobs: rescan the ingest folder and resync Calibre on a timer (Settings sets the intervals)."""
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db, ingest, library


def _minutes_since(ts: str | None) -> float:
    if not ts:
        return float("inf")
    then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


class Scheduler:
    TICK = 20  # seconds

    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _active(self, conn, kind: str) -> bool:
        return conn.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                            (kind,)).fetchone() is not None

    def tick(self) -> list[str]:
        """Submit whatever is due. Returns the job kinds submitted (for tests)."""
        submitted = []
        conn = db.connect(self.settings.db_path)
        try:
            checks = (
                ("ingest_scan", "ingest_interval_min", "ingest_scanned_at", {}),
                ("library_sync", "library_sync_interval_min", "library_synced_at", {}),
            )
            for kind, setting, last_key, params in checks:
                try:
                    every = float(ingest.setting(conn, setting))
                except ValueError:
                    continue
                if every <= 0 or self._active(conn, kind):
                    continue
                # Only schedule what can work: an unmounted folder would just fail every interval.
                if kind == "ingest_scan" and not Path(ingest.setting(conn, "ingest_path")).is_dir():
                    continue
                if kind == "library_sync" and not (Path(library.calibre_path(conn)) / "metadata.db").is_file():
                    continue
                # don't retry a failing job every tick: count the last attempt, not just the last success
                last = conn.execute("SELECT max(created_at) FROM jobs WHERE kind=?", (kind,)).fetchone()[0]
                if min(_minutes_since(db.get_meta(conn, last_key)), _minutes_since(last)) >= every:
                    self.runner.submit(kind, {**params, "scheduled": True})
                    submitted.append(kind)
        finally:
            conn.close()
        return submitted

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001  -- the scheduler must keep running
                pass
