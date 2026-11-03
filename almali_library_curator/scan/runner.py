"""Single-process and cross-process safe scan orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import logging
import json
import sqlite3
from threading import Thread

from almali_library_curator.config import Settings, load_settings
from almali_library_curator.db import connect
from almali_library_curator.scan.lock import ScanLock

logger = logging.getLogger(__name__)


def local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class ScanReport:
    status: str
    trigger: str
    mode: str
    started_at: str
    finished_at: str
    candidates_found: int = 0
    error: str | None = None
    steps: dict = field(default_factory=dict)
    id: int | None = None


def last_scan(conn):
    """Newest successful run, never a skipped/failed attempt."""
    row = conn.execute(
        "SELECT * FROM scan_runs WHERE status = 'finished' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


class ScanRunner:
    def __init__(self, settings: Settings, *, steps_factory=None, enrichment_step=None,
                 clock=local_now, settings_loader=load_settings):
        self.settings = settings
        if steps_factory is None:
            from almali_library_curator.scan.pipeline import default_steps
            steps_factory = default_steps
        self.steps_factory = steps_factory
        self.enrichment_step = enrichment_step
        self.clock = clock
        self.settings_loader = settings_loader
        self.thread = None

    def run(self, trigger: str) -> ScanReport:
        if trigger not in ("manual", "schedule"):
            raise ValueError("trigger must be manual or schedule")
        lock = ScanLock(self.settings.config_dir)
        if not lock.acquire():
            now = self.clock().isoformat()
            return self._record(ScanReport("skipped", trigger,
                                "dry_run" if self.settings.dry_run else "live", now, now))
        try:
            return self._run_locked(trigger)
        finally:
            lock.release()

    def start_background(self) -> bool:
        """Reserve synchronously, so concurrent POSTs cannot both return 202."""
        lock = ScanLock(self.settings.config_dir)
        if not lock.acquire():
            return False

        def work():
            try:
                self._run_locked("manual")
            except Exception as error:
                logger.warning("scan_background_failed", extra={"error": type(error).__name__})
            finally:
                lock.release()

        self.thread = Thread(target=work, name="scan-manual", daemon=True)
        try:
            self.thread.start()
        except Exception:
            lock.release()
            raise
        return True

    def _run_locked(self, trigger):
        started = self.clock().isoformat()
        try:
            return self._execute(trigger)
        except Exception as error:
            diagnostic = type(error).__name__
            logger.warning("scan_failed", extra={"error": diagnostic})
            return self._record(ScanReport(
                "failed", trigger, "dry_run" if self.settings.dry_run else "live",
                started, self.clock().isoformat(), error=diagnostic,
            ))

    def _execute(self, trigger):
        started = self.clock().isoformat()
        settings = self.settings_loader(self.settings.config_dir)
        if trigger == "schedule" and self._slot_finished(settings):
            return self._record(ScanReport(
                "skipped", trigger, "dry_run" if settings.dry_run else "live",
                started, self.clock().isoformat(),
            ))
        conn = connect(self.settings.database_path)
        if settings.dry_run:
            # Sync/enrichment commit internally. A private snapshot allows every
            # read/compute step without leaking any preview into the live DB.
            preview = sqlite3.connect(":memory:")
            preview.row_factory = sqlite3.Row
            conn.backup(preview)
            conn.close()
            conn = preview
        try:
            steps = self.steps_factory(conn, settings, self.clock, self.enrichment_step)
            results = {}
            errors = []
            for name in ("sync", "enrichment", "detection", "recheck", "deletion"):
                if name == "deletion" and any(
                    results[dependency]["status"] == "failed"
                    for dependency in ("sync", "detection", "recheck")
                ):
                    results[name] = {"status": "skipped"}
                    continue
                try:
                    output = steps[name]()
                    results[name] = {"status": "finished", "counts": output}
                    diagnostics = output.get("errors", {}) if isinstance(output, dict) else {}
                    if diagnostics:
                        diagnostic = ", ".join(sorted(set(diagnostics.values())))
                        errors.append(diagnostic)
                        results[name].update(status="failed", error=diagnostic)
                except Exception as error:
                    conn.rollback()
                    diagnostic = type(error).__name__
                    errors.append(diagnostic)
                    results[name] = {"status": "failed", "error": diagnostic}
                    logger.warning("scan_step_failed", extra={"step": name, "error": diagnostic})
            detection = results.get("detection", {}).get("counts") or {}
            return self._record(ScanReport(
                "failed" if errors else "finished", trigger,
                "dry_run" if settings.dry_run else "live", started,
                self.clock().isoformat(),
                candidates_found=detection.get("candidates", detection.get("count", 0)),
                error=", ".join(errors) or None, steps=results,
            ))
        finally:
            conn.close()

    def _slot_finished(self, settings: Settings) -> bool:
        """This check is under scan.lock, closing the cross-worker race."""
        conn = connect(self.settings.database_path)
        try:
            today = self.clock().date()
            rows = conn.execute(
                "SELECT started_at FROM scan_runs WHERE status = 'finished'"
                " AND trigger = 'schedule'"
            )
            for row in rows:
                moment = datetime.fromisoformat(row["started_at"])
                if moment.astimezone(self.clock().tzinfo).date() == today:
                    return True
            return False
        finally:
            conn.close()

    def _record(self, report):
        conn = connect(self.settings.database_path)
        try:
            cursor = conn.execute(
                "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger,"
                " candidates_found, error, report) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (report.started_at, report.finished_at, report.status, report.mode,
                 report.trigger, report.candidates_found, report.error,
                 json.dumps(report.steps)),
            )
            conn.commit()
            return replace(report, id=cursor.lastrowid)
        finally:
            conn.close()
