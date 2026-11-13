"""Single-process and cross-process safe scan orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
import json
import sqlite3
from threading import Thread

from almali_library_curator.config import Settings
from almali_library_curator.scan.settings import load_runtime_settings
from almali_library_curator.db import SCAN_REPORT_RETENTION, connect
from almali_library_curator.scan.lock import ScanLock

logger = logging.getLogger(__name__)


def local_now() -> datetime:
    return datetime.now().astimezone()


#: Enrichment diagnostics whose failure makes this run's play history or arr
#: ownership unknown, so deletion must be skipped for the run (#49). These
#: errors only occur when the failing service is configured.
DELETION_BLOCKING_ENRICHMENT_ERRORS = ("import_tautulli_history", "link_arr")


def enrichment_blocks_deletion(enrichment: dict) -> bool:
    """True when the enrichment step failed in a way deletion cannot ignore.

    A recorded diagnostic of a configured service's history import or arr
    linking blocks the run's deletion (#49). A step that raised outright
    blocks too: the failing operation is unknown, and an unknown history must
    not drive a deletion. Other diagnostics stay non-blocking; the deletion
    executor still refuses to claim a file it could not see before the call.
    """
    if enrichment.get("status") != "failed":
        return False
    errors = (enrichment.get("counts") or {}).get("errors") or {}
    if not errors:
        return True
    return any(error == DELETION_BLOCKING_ENRICHMENT_ERRORS[0]
               or error.startswith(DELETION_BLOCKING_ENRICHMENT_ERRORS[1])
               for error in errors)


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
    """Labels of the newest successful run, never its potentially large report."""
    row = conn.execute(
        "SELECT id, started_at, finished_at, status, mode, trigger, candidates_found, error"
        " FROM scan_runs WHERE status = 'finished' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


class ScanRunner:
    def __init__(self, settings: Settings, *, steps_factory=None, enrichment_step=None,
                 clock=local_now, settings_loader=load_runtime_settings):
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
                if name == "deletion" and self._deletion_blocked(results):
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

    def _deletion_blocked(self, results: dict) -> bool:
        """Deletion only runs on fully known facts: core steps or a configured
        service's import/linking must not have failed this run."""
        if any(results[dependency]["status"] == "failed"
               for dependency in ("sync", "detection", "recheck")):
            return True
        return enrichment_blocks_deletion(results.get("enrichment") or {})

    def _slot_finished(self, settings: Settings) -> bool:
        """This check is under scan.lock, closing the cross-worker race."""
        conn = connect(self.settings.database_path)
        try:
            now = self.clock()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            row = conn.execute(
                "SELECT id FROM scan_runs WHERE status = 'finished'"
                " AND trigger = 'schedule'"
                " AND unixepoch(started_at) >= ? AND unixepoch(started_at) < ?"
                " ORDER BY id DESC LIMIT 1",
                (int(day_start.timestamp()), int(day_end.timestamp())),
            ).fetchone()
            return row is not None
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
            conn.execute(
                "UPDATE scan_runs SET report = NULL WHERE report IS NOT NULL AND id NOT IN"
                " (SELECT id FROM scan_runs ORDER BY id DESC LIMIT ?)",
                (SCAN_REPORT_RETENTION,),
            )
            conn.execute(
                "DELETE FROM scan_run_views WHERE run_id NOT IN"
                " (SELECT id FROM scan_runs ORDER BY id DESC LIMIT ?)"
                " AND run_id != COALESCE((SELECT MAX(id) FROM scan_runs"
                " WHERE status = 'finished'), 0)",
                (SCAN_REPORT_RETENTION,),
            )
            conn.commit()
            # VACUUM works on existing non-auto-vacuum databases too. It must
            # run outside the recording transaction. Amortize this exclusive
            # write phase over 30 attempts, never across any remote fetching.
            if cursor.lastrowid > SCAN_REPORT_RETENTION and cursor.lastrowid % SCAN_REPORT_RETENTION == 0:
                try:
                    conn.execute("VACUUM")
                except sqlite3.OperationalError:
                    logger.warning("scan_report_vacuum_deferred")
            return replace(report, id=cursor.lastrowid)
        finally:
            conn.close()
