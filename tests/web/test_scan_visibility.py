"""The scan outcome is visible: running, finished or failed (S2).

The owner's field test pressed the scan button, the run failed in the sync
step with a ``ValidationError`` and nothing in the interface said so. These
tests use the real runner, a real migrated database in a temporary directory
and plain step functions, the same way tests/web/test_scan.py does.
"""
from __future__ import annotations

from threading import Event

import pytest

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings, save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.scan import ScanRunner

STEP_NAMES = ("sync", "enrichment", "detection", "recheck", "deletion")


class ValidationError(Exception):
    """The same diagnostic the owner's real run recorded."""


def _steps(sync):
    """A real steps factory whose sync step is the one under test."""
    def factory(*args):
        return {"sync": sync, **{name: (lambda: {}) for name in
                                 ("enrichment", "detection", "recheck", "deletion")}}
    return factory


def _app(tmp_path, sync, **runner_kwargs):
    settings = Settings(config_dir=tmp_path / "config", scheduler_enabled=False)
    save_settings(settings)
    runner = ScanRunner(settings, steps_factory=_steps(sync), **runner_kwargs)
    app = create_app(settings, scan_runner=runner)
    app.config.update(TESTING=True, SECRET_KEY="web-test-session", WTF_CSRF_ENABLED=False)
    return app, runner


def test_a_failed_scan_names_its_steps_and_never_the_exception(tmp_path, login):
    app, runner = _app(tmp_path, lambda: (_ for _ in ()).throw(ValidationError("part")))
    client = app.test_client()
    assert login(client).status_code == 303
    report = runner.run("manual")
    assert report.status == "failed"  # the run is recorded failed, like the owner's

    html = client.get("/?lang=en").get_data(as_text=True)
    assert translate("scan.status.failed", "en") in html
    for name in STEP_NAMES:
        assert translate(f"scan.step.{name}", "en") in html
    assert translate("scan.status.failed_hint", "en") in html
    # The recorded diagnostic is a bare class name; it never reaches the page.
    assert "ValidationError" not in html
    # The failure pairs its icon with words and offers the retry action.
    assert translate("action.scan", "en") in html


def test_a_running_scan_says_so_on_the_screen(tmp_path, login):
    entered, release = Event(), Event()

    def sync():
        entered.set()
        assert release.wait(5)
        return {}

    app, runner = _app(tmp_path, sync)
    client = app.test_client()
    assert login(client).status_code == 303
    try:
        assert client.post("/actions/scan").status_code == 303
        assert entered.wait(5)
        html = client.get("/?lang=en").get_data(as_text=True)
        assert translate("scan.status.running", "en") in html
        assert translate("scan.status.failed", "en") not in html
    finally:
        release.set()
        runner.thread.join(5)
    assert not runner.thread.is_alive()


def test_a_finished_scan_lists_its_steps(tmp_path, login):
    app, runner = _app(tmp_path, lambda: {})
    client = app.test_client()
    assert login(client).status_code == 303
    assert runner.run("manual").status == "finished"

    html = client.get("/?lang=en").get_data(as_text=True)
    assert translate("scan.status.finished", "en") in html
    for name in STEP_NAMES:
        assert translate(f"scan.step.{name}", "en") in html
    assert translate("scan.status.failed", "en") not in html


@pytest.mark.parametrize("locale", ["en", "ca", "es"])
def test_a_failed_scan_never_promises_that_nothing_was_deleted(locale):
    # A run can fail after its deletion step removed something (a later
    # item, or the final record), so the failed state points to History
    # instead of guaranteeing that nothing was deleted.
    text = translate("scan.status.failed", locale)
    for promise in ("Nothing was deleted", "No s'ha esborrat res", "No se ha borrado nada"):
        assert promise not in text
    assert translate("nav.history", locale) in text
