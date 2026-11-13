"""Scan orchestration contracts with injected boundary steps (no W8a import)."""
from dataclasses import replace
from datetime import datetime, timezone
import json

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db


NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def test_run_reloads_settings_and_orders_steps(tmp_path):
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(replace(settings, grace_days=30))
    init_db(settings.database_path).close()
    observed = []

    def factory(conn, loaded, clock, enrichment):
        assert loaded.grace_days == 30
        return {name: lambda name=name: observed.append(name) or {"count": 2}
                for name in ("sync", "enrichment", "detection", "recheck", "deletion")}

    report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
    assert observed == ["sync", "enrichment", "detection", "recheck", "deletion"]
    assert report.status == "finished"


def test_failed_steps_are_recorded_and_only_dependent_deletion_is_skipped(tmp_path):
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    for failure in ("sync", "enrichment", "detection", "recheck", "deletion"):
        observed = []

        def factory(conn, loaded, clock, enrichment):
            def step(name):
                observed.append(name)
                if name == failure:
                    raise RuntimeError("secret-must-not-be-stored")
                return {"count": 1}
            return {name: lambda name=name: step(name) for name in
                    ("sync", "enrichment", "detection", "recheck", "deletion")}

        report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
        assert report.status == "failed"
        assert report.steps[failure]["error"] == "RuntimeError"
        assert "secret-must-not-be-stored" not in repr(report)
        assert "recheck" in observed  # Isolated failure does not starve this step.
        if failure in ("sync", "detection", "recheck"):
            assert "deletion" not in observed
        elif failure == "enrichment":
            # S3 (#49): an enrichment step that raised leaves the play history
            # and the arr ownership unknown, so the run must not delete.
            assert "deletion" not in observed


def test_lock_contention_between_two_runners_does_no_pipeline_work(tmp_path):
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    nested = []
    forbidden = []
    second = ScanRunner(settings, steps_factory=lambda *args: forbidden.append(True))

    def factory(*args):
        def sync():
            nested.append(second.run("manual"))
            return {}
        return {"sync": sync, **{name: lambda: {} for name in
                                  ("enrichment", "detection", "recheck", "deletion")}}

    report = ScanRunner(settings, steps_factory=factory).run("manual")
    assert report.status == "finished"
    assert nested[0].status == "skipped"
    assert forbidden == []
    assert (tmp_path / "scan.lock").exists()


def test_scan_rows_and_last_finished_scan(tmp_path):
    from almali_library_curator.scan import ScanRunner, last_scan
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    assert last_scan(conn) is None
    factory = lambda *args: {name: lambda: {"count": 2} for name in
                            ("sync", "enrichment", "detection", "recheck", "deletion")}
    runner = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW)
    report = runner.run("manual")
    row = last_scan(conn)
    assert row["id"] == report.id
    assert row["trigger"] == "manual"
    assert row["status"] == "finished"
    assert row["mode"] == "live"
    assert row["started_at"] == row["finished_at"] == NOW.isoformat()
    assert row["candidates_found"] == 2
    # The labels carry the bounded step summary, never the full snapshot.
    assert json.loads(row["report"]) == {"sync": "finished", "enrichment": "finished",
                                         "detection": "finished", "recheck": "finished",
                                         "deletion": "finished"}
    stored = conn.execute('SELECT report FROM scan_runs WHERE id=?', (report.id,)).fetchone()
    assert json.loads(stored['report'])["sync"]["counts"]["count"] == 2
    from almali_library_curator.scan.lock import ScanLock
    lock = ScanLock(tmp_path)
    assert lock.acquire()
    try:
        skipped = runner.run("manual")
    finally:
        lock.release()
    assert skipped.status == "skipped"
    assert conn.execute("SELECT status FROM scan_runs ORDER BY id DESC").fetchone()[0] == "skipped"
    assert last_scan(conn)["id"] == report.id
    conn.close()


def test_dry_run_isolates_sync_and_enrichment_database_writes(tmp_path):
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    conn.execute("INSERT INTO app_meta VALUES ('sentinel', 'unchanged')")
    conn.commit()
    before = [line for line in conn.iterdump() if 'scan_run' not in line and 'sqlite_sequence' not in line]

    def factory(work, loaded, clock, enrichment):
        def sync():
            work.execute("INSERT INTO users (plex_user_id, title) VALUES ('1', 'Marc')")
            work.commit()
            return {"users": 1}
        def enrich():
            assert work.execute("SELECT title FROM users").fetchone()[0] == "Marc"
            work.execute("UPDATE app_meta SET value = 'preview' WHERE key = 'sentinel'")
            work.commit()
            return {"files": 1}
        return {"sync": sync, "enrichment": enrich, **{name: lambda: {} for name in
                                                     ("detection", "recheck", "deletion")}}

    report = ScanRunner(settings, steps_factory=factory).run("manual")
    assert report.status == "finished"
    after = [line for line in conn.iterdump() if 'scan_run' not in line and 'sqlite_sequence' not in line]
    assert after == before
    assert conn.execute("SELECT count(*) FROM scan_runs").fetchone()[0] == 1
    conn.close()


def test_failed_settings_reload_and_factory_are_audited_and_release_lock(tmp_path):
    from almali_library_curator.scan import ScanRunner, last_scan
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)

    def broken(*args):
        raise ValueError("secret-must-not-be-stored")

    for kwargs in ({"settings_loader": broken}, {"steps_factory": broken}):
        report = ScanRunner(settings, **kwargs).run("manual")
        assert report.status == "failed"
        assert report.error == "ValueError"
        assert last_scan(conn) is None
    rows = conn.execute("SELECT * FROM scan_runs").fetchall()
    assert len(rows) == 2
    assert "secret-must-not-be-stored" not in repr([tuple(row) for row in rows])
    conn.close()


def _hold_scan_in_child(settings, entered, release):
    from almali_library_curator.scan import ScanRunner
    def factory(*args):
        def sync():
            entered.set()
            assert release.wait(10)
            return {}
        return {"sync": sync, **{name: lambda: {} for name in
                                  ("enrichment", "detection", "recheck", "deletion")}}
    assert ScanRunner(settings, steps_factory=factory).run("manual").status == "finished"


def test_file_lock_excludes_a_separate_process(tmp_path):
    import multiprocessing
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    child = context.Process(target=_hold_scan_in_child, args=(settings, entered, release))
    child.start()
    try:
        assert entered.wait(5)
        report = ScanRunner(settings, steps_factory=lambda *args: {}).run("manual")
        assert report.status == "skipped"
    finally:
        release.set()
        child.join(10)
    assert child.exitcode == 0


def test_tautulli_history_import_error_blocks_deletion_for_that_run(tmp_path):
    """S3 (#49): a configured service's failed history import skips deletion.

    Without the fresh history the final re-check cannot see recent plays, so a
    deletion decision would be made on stale facts.
    """
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    observed = []

    def factory(*args):
        return {"enrichment": lambda: {"history_imported": 0,
                                       "errors": {"import_tautulli_history": "ConnectionError"}},
                **{name: lambda name=name: observed.append(name) or {} for name in
                   ("sync", "detection", "recheck", "deletion")}}

    report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
    assert report.status == "failed"
    assert report.steps["enrichment"]["error"] == "ConnectionError"
    assert report.steps["deletion"]["status"] == "skipped"
    assert "recheck" in observed
    assert "deletion" not in observed


def test_arr_linking_error_blocks_deletion_for_that_run(tmp_path):
    """S3 (#49): a configured arr whose linking failed skips deletion."""
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    observed = []

    def factory(*args):
        return {"enrichment": lambda: {"linked": 0,
                                       "errors": {"link_arr.sonarr": "ConnectionError"}},
                **{name: lambda name=name: observed.append(name) or {} for name in
                   ("sync", "detection", "recheck", "deletion")}}

    report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
    assert report.status == "failed"
    assert report.steps["enrichment"]["error"] == "ConnectionError"
    assert report.steps["deletion"]["status"] == "skipped"
    assert "deletion" not in observed


def test_unrelated_enrichment_errors_do_not_starve_deletion(tmp_path):
    """Only import/link failures block deletion; other optional diagnostics
    are audited without skipping the run's deletion pass."""
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    observed = []

    def factory(*args):
        return {"enrichment": lambda: {"files": 3, "errors": {"refresh_files": "OSError"}},
                **{name: lambda name=name: observed.append(name) or {} for name in
                   ("sync", "detection", "recheck", "deletion")}}

    report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
    assert report.status == "failed"
    assert report.steps["enrichment"]["error"] == "OSError"
    assert report.steps["deletion"]["status"] == "finished"
    assert "deletion" in observed


def test_an_enrichment_step_that_raised_blocks_deletion(tmp_path):
    """S3 (#49): the step raised, so which operation failed is unknown.

    An unknown history must not drive a deletion, so the run skips it.
    """
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    init_db(settings.database_path).close()
    observed = []

    def factory(*args):
        def step(name):
            observed.append(name)
            if name == "enrichment":
                raise RuntimeError("secret-must-not-be-stored")
            return {"count": 1}
        return {name: lambda name=name: step(name) for name in
                ("sync", "enrichment", "detection", "recheck", "deletion")}

    report = ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run("manual")
    assert report.status == "failed"
    assert report.steps["enrichment"]["error"] == "RuntimeError"
    assert report.steps["deletion"]["status"] == "skipped"
    assert "recheck" in observed
    assert "deletion" not in observed
