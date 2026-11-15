"""The periodic VACUUM yields the writer slot instead of queueing for it (#84).

`Runner._record` runs `VACUUM` every 30th recorded attempt on the same
connection that opens with a 30 s busy timeout. A held writer must therefore
make maintenance defer quickly, not stall the recording (and every other web
write) for up to 30 s. The successful, uncontended rebuild stays pinned by
`tests/scan/test_report_retention.py::test_reports_are_bounded_and_space_is_reclaimed_on_an_existing_database`.
"""
import logging
import sqlite3
import time

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import SCAN_REPORT_RETENTION, init_db
from almali_library_curator.scan import ScanRunner
from almali_library_curator.scan import runner as runner_module

STEPS = ('sync', 'enrichment', 'detection', 'recheck', 'deletion')


def _steps_factory(*args):
    return {name: lambda: {} for name in STEPS}


def _seed_history(conn):
    """Idle rows so the next recording is the one that triggers maintenance."""
    for _ in range(SCAN_REPORT_RETENTION * 2 - 1):
        conn.execute("INSERT INTO scan_runs (status) VALUES ('failed')")
    conn.commit()


def _install_probe(monkeypatch, database_path, observed, hold_writer):
    """Start a competing writer exactly when the maintenance step runs.

    Holding that writer across the whole recording would block the attempt
    INSERT itself, so the lock has to appear at the VACUUM, not before it.
    """
    class Probe(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.strip().upper().startswith('VACUUM'):
                observed['during'] = super().execute(
                    'PRAGMA busy_timeout').fetchone()[0]
                holder = sqlite3.connect(database_path, timeout=30.0,
                                         isolation_level=None)
                if hold_writer:
                    holder.execute('BEGIN IMMEDIATE')
                try:
                    cursor = super().execute(sql, parameters)
                    observed['rebuilt'] = True
                    return cursor
                finally:
                    if hold_writer:
                        holder.execute('ROLLBACK')
                    holder.close()
            return super().execute(sql, parameters)

        def close(self):
            observed['after'] = super().execute(
                'PRAGMA busy_timeout').fetchone()[0]
            super().close()

    monkeypatch.setattr(runner_module, 'connect',
                        lambda path: Probe(str(path), timeout=30.0))


def test_vacuum_defers_instead_of_holding_the_writer(tmp_path, monkeypatch, caplog):
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    _seed_history(conn)
    observed = {}
    _install_probe(monkeypatch, settings.database_path, observed, hold_writer=True)
    runner = ScanRunner(settings, steps_factory=_steps_factory)
    with caplog.at_level(logging.WARNING, logger='almali_library_curator.scan.runner'):
        started = time.monotonic()
        report = runner.run('manual')
        elapsed = time.monotonic() - started
    # The recording is not queued behind the held writer for the whole 30 s.
    assert elapsed < 5
    # Maintenance asks for the writer slot briefly, then gives it back.
    assert observed['during'] <= 1_000
    assert observed['after'] == 30_000
    # A deferred rebuild is a logged maintenance decision, not a scan failure.
    assert 'scan_report_vacuum_deferred' in caplog.text
    assert report.status == 'finished'
    assert conn.execute('SELECT status FROM scan_runs WHERE id = ?',
                        (report.id,)).fetchone()[0] == 'finished'
    assert conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0] == \
        SCAN_REPORT_RETENTION * 2
    conn.close()


def test_uncontended_vacuum_still_rebuilds_and_restores_the_timeout(
        tmp_path, monkeypatch, caplog):
    """The short timeout is maintenance-scoped: it never leaks to the caller."""
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    _seed_history(conn)
    observed = {}
    _install_probe(monkeypatch, settings.database_path, observed, hold_writer=False)
    runner = ScanRunner(settings, steps_factory=_steps_factory)
    with caplog.at_level(logging.WARNING, logger='almali_library_curator.scan.runner'):
        report = runner.run('manual')
    assert observed['rebuilt'] is True
    assert observed['during'] == 1_000
    assert observed['after'] == 30_000
    assert 'scan_report_vacuum_deferred' not in caplog.text
    assert report.status == 'finished'
    conn.close()
