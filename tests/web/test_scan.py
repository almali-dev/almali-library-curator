"""POST /scan reserves the same cross-worker slot before answering."""
from threading import Event

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings, save_settings


def test_scan_endpoint_returns_202_then_409_while_running(tmp_path, login):
    from almali_library_curator.scan import ScanRunner
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False, dry_run=False)
    save_settings(settings)
    entered, release = Event(), Event()

    def factory(*args):
        def sync():
            entered.set()
            assert release.wait(5)
            return {}
        return {"sync": sync, **{name: lambda: {} for name in
                                  ("enrichment", "detection", "recheck", "deletion")}}

    runner = ScanRunner(settings, steps_factory=factory)
    app = create_app(settings, scan_runner=runner)
    app.config["WTF_CSRF_ENABLED"] = False  # the CSRF guard is covered in test_csrf.py
    client = app.test_client()
    assert login(client).status_code == 303  # POST /scan needs the owner's session (S17)
    try:
        assert client.post("/scan").status_code == 202
        assert entered.wait(5)
        # A different app/runner represents another Gunicorn worker.
        other = create_app(settings, scan_runner=ScanRunner(settings, steps_factory=factory))
        other.config["WTF_CSRF_ENABLED"] = False
        other_client = other.test_client()
        assert login(other_client).status_code == 303
        assert other_client.post("/scan").status_code == 409
        assert client.get("/scan").status_code == 405
    finally:
        release.set()
        runner.thread.join(5)
    assert not runner.thread.is_alive()
    assert client.post("/scan").status_code == 202
    runner.thread.join(5)


def test_application_starts_scheduler_only_when_enabled(tmp_path, monkeypatch):
    from almali_library_curator.scan.scheduler import ScanScheduler
    started = []
    monkeypatch.setattr(ScanScheduler, "start", lambda self: started.append(self))
    disabled = Settings(config_dir=tmp_path / "disabled", scheduler_enabled=False)
    create_app(disabled)
    assert started == []
    enabled = Settings(config_dir=tmp_path / "enabled", scheduler_enabled=True)
    create_app(enabled)
    assert len(started) == 1
