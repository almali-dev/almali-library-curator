"""Daily slot calculation uses an injected local clock."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db


def test_scheduler_slots_before_at_and_after_local_time():
    from almali_library_curator.scan.scheduler import next_slot
    now = datetime(2026, 11, 2, 5, 30, tzinfo=timezone(timedelta(hours=2)))
    slot = now.replace(hour=6, minute=0)
    assert next_slot(now, "06:00") == slot
    assert next_slot(slot, "06:00") == slot
    assert next_slot(slot + timedelta(seconds=1), "06:00") == slot + timedelta(days=1)


def test_two_schedulers_share_finished_daily_slot_and_manual_is_independent(tmp_path):
    from almali_library_curator.scan import ScanRunner
    from almali_library_curator.scan.scheduler import ScanScheduler
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=True)
    save_settings(settings)
    conn = init_db(settings.database_path)
    now = [datetime(2026, 11, 2, 5, tzinfo=timezone.utc)]
    calls = []

    def factory(*args):
        return {name: lambda name=name: calls.append(name) or {} for name in
                ("sync", "enrichment", "detection", "recheck", "deletion")}

    first = ScanRunner(settings, steps_factory=factory, clock=lambda: now[0])
    second = ScanRunner(settings, steps_factory=factory, clock=lambda: now[0])
    one = ScanScheduler(first, clock=lambda: now[0])
    two = ScanScheduler(second, clock=lambda: now[0])
    assert one.tick() is None
    now[0] = now[0].replace(hour=6)
    assert one.tick().status == "finished"
    assert two.tick().status == "skipped"
    assert len(calls) == 5
    assert one.tick() is None
    assert second.run("manual").status == "finished"
    now[0] += timedelta(days=1)
    assert one.tick().status == "finished"
    assert len(calls) == 15
    conn.close()


def test_scheduler_thread_is_daemon_and_can_be_started_explicitly(tmp_path):
    from almali_library_curator.scan import ScanRunner
    from almali_library_curator.scan.scheduler import ScanScheduler
    settings = Settings(config_dir=tmp_path, scheduler_enabled=True)
    save_settings(settings)
    init_db(settings.database_path).close()
    entered = Event()

    def factory(*args):
        return {name: lambda: entered.set() or {} for name in
                ("sync", "enrichment", "detection", "recheck", "deletion")}

    now = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    runner = ScanRunner(settings, clock=lambda: now, steps_factory=factory)
    scheduler = ScanScheduler(runner, clock=lambda: now)
    try:
        scheduler.start()
        assert entered.wait(5)
        assert scheduler.thread.daemon
    finally:
        scheduler.stop()
    assert not scheduler.thread.is_alive()
