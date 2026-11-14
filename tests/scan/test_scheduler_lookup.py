"""The scheduler finds today's finished slot without scanning every run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import almali_library_curator.scan.runner as runner_module
from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db


def _settings(tmp_path) -> Settings:
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    return settings


def _run_row(conn, started_at: datetime, *, status="finished", trigger="schedule") -> None:
    conn.execute(
        "INSERT INTO scan_runs (started_at, status, trigger) VALUES (?, ?, ?)",
        (started_at.isoformat(), status, trigger),
    )
    conn.commit()


def test_todays_finished_slot_is_found_even_when_a_later_old_row_exists(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    conn = init_db(settings.database_path)
    _run_row(conn, now)
    _run_row(conn, now - timedelta(days=1))
    conn.close()
    calls = []

    report = runner_module.ScanRunner(
        settings,
        clock=lambda: now,
        steps_factory=lambda *args: {
            name: lambda name=name: calls.append(name) or {}
            for name in ("sync", "enrichment", "detection", "recheck", "deletion")
        },
    ).run("schedule")

    assert report.status == "skipped"
    assert calls == []


def test_manual_and_unfinished_runs_do_not_claim_todays_scheduled_slot(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    conn = init_db(settings.database_path)
    _run_row(conn, now - timedelta(days=1))
    _run_row(conn, now, status="failed")
    _run_row(conn, now, status="skipped")
    _run_row(conn, now, trigger="manual")
    conn.close()
    calls = []

    report = runner_module.ScanRunner(
        settings,
        clock=lambda: now,
        steps_factory=lambda *args: {
            name: lambda name=name: calls.append(name) or {}
            for name in ("sync", "enrichment", "detection", "recheck", "deletion")
        },
    ).run("schedule")

    assert report.status == "finished"
    assert calls == ["sync", "enrichment", "detection", "recheck", "deletion"]


def test_schedule_slot_lookup_uses_one_limited_indexed_statement(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    now = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    conn = init_db(settings.database_path)
    for day in range(500):
        _run_row(conn, now - timedelta(days=day + 1))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slot_sql = (
        "SELECT id FROM scan_runs WHERE status = 'finished'"
        " AND trigger = 'schedule'"
        " AND unixepoch(started_at) >= ? AND unixepoch(started_at) < ?"
        " ORDER BY id DESC LIMIT 1"
    )
    plan = conn.execute(
        "EXPLAIN QUERY PLAN " + slot_sql,
        (int(day_start.timestamp()), int((day_start + timedelta(days=1)).timestamp())),
    ).fetchall()
    assert any("idx_scan_runs_schedule_slot" in row[3] for row in plan)
    conn.close()

    statements = []
    original_connect = runner_module.connect

    def traced_connect(path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(runner_module, "connect", traced_connect)

    report = runner_module.ScanRunner(
        settings,
        clock=lambda: now,
        steps_factory=lambda *args: {
            name: lambda: {}
            for name in ("sync", "enrichment", "detection", "recheck", "deletion")
        },
    ).run("schedule")

    slot_queries = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FROM scan_runs" in statement
    ]
    assert report.status == "finished"
    assert len(slot_queries) == 1
    assert "ORDER BY id DESC LIMIT 1" in slot_queries[0]
