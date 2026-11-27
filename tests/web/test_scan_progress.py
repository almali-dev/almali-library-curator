"""The page follows a running analysis (S2, #147).

The status box promised that the outcome appears there, but nothing refreshed
it. While a scan runs, the box polls a small JSON endpoint behind the login
and the page reloads once when the scan finishes or fails. Without JavaScript
the copy says to reload the page.

The tests use the real runner, a real migrated database in a temporary
directory and plain step functions, the same way tests/web/test_scan.py and
tests/web/test_scan_visibility.py do.
"""
from __future__ import annotations

from threading import Event

from flask.testing import FlaskClient
from markupsafe import Markup
import pytest

from almali_library_curator.app import create_app
from almali_library_curator.auth import PUBLIC_ENDPOINTS
from almali_library_curator.config import Settings, save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.scan import ScanRunner

STEP_NAMES = ("sync", "enrichment", "detection", "recheck", "deletion")
# The three places that promised the outcome appears on its own.
PROMISING_KEYS = ("scan.status.running", "empty.candidates_never_running",
                  "setup.libraries_scan_running")


def _steps(sync):
    def factory(*args):
        return {"sync": sync, **{name: (lambda: {}) for name in STEP_NAMES[1:]}}
    return factory


def _app(tmp_path, sync, **runner_kwargs):
    settings = Settings(config_dir=tmp_path / "config", scheduler_enabled=False)
    save_settings(settings)
    runner = ScanRunner(settings, steps_factory=_steps(sync), **runner_kwargs)
    app = create_app(settings, scan_runner=runner)
    app.config.update(TESTING=True, SECRET_KEY="web-test-session", WTF_CSRF_ENABLED=False)
    return app, runner


def test_a_running_scan_offers_the_status_endpoint_and_its_script(tmp_path, login):
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
        page = client.get("/?lang=en")
        html = page.get_data(as_text=True)
        assert 'id="scan-status"' in html
        assert 'data-status-url="/scan/status?lang=en"' in html
        assert "js/scan-status.js" in html
        assert translate("scan.status.running", "en") in " ".join(
            Markup(html).striptags().split())
    finally:
        release.set()
        runner.thread.join(5)
    assert not runner.thread.is_alive()


def test_the_status_endpoint_answers_only_the_state(tmp_path, login):
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
        running = client.get("/scan/status")
        assert running.status_code == 200
        assert running.json == {"state": "running", "running": True}
    finally:
        release.set()
        runner.thread.join(5)
    finished = client.get("/scan/status")
    assert finished.json == {"state": "finished", "running": False}


def test_a_failed_scan_answers_the_failure_state(tmp_path, login):
    class ValidationError(Exception):
        pass

    def sync():
        raise ValidationError("part")

    app, runner = _app(tmp_path, sync)
    client = app.test_client()
    assert login(client).status_code == 303
    assert runner.run("manual").status == "failed"

    response = client.get("/scan/status")
    assert response.status_code == 200
    assert response.json == {"state": "failed", "running": False}
    # Only status words: never a report, a token or a path.
    assert set(response.json) == {"state", "running"}


def test_the_status_endpoint_is_behind_the_owner_login(tmp_path):
    app, _runner = _app(tmp_path, lambda: {})
    client = FlaskClient(app)

    response = client.get("/scan/status")

    assert response.status_code == 303
    assert "/login" in response.headers["Location"]
    assert any(rule.rule == "/scan/status" for rule in app.url_map.iter_rules())
    assert "web.scan_status" not in PUBLIC_ENDPOINTS


def test_the_polling_script_stops_and_reloads_once(client):
    script = client.get("/static/js/scan-status.js")

    assert script.status_code == 200
    body = script.get_data(as_text=True)
    assert "data-status-url" in body
    assert "setInterval" in body and "clearInterval" in body
    # One reload, behind a guard, and only after a poll answered.
    assert body.count("location.reload") == 1
    assert "reloaded" in body
    assert "fetch(" in body


@pytest.mark.parametrize("locale,phrase", [
    ("en", "reload the page"),
    ("ca", "recarrega la pàgina"),
    ("es", "recarga la página"),
])
def test_the_copy_without_javascript_tells_the_owner_to_reload(locale, phrase):
    for key in PROMISING_KEYS:
        assert phrase in translate(key, locale), (locale, key)


def test_the_promised_outcome_and_the_reload_advice_live_in_the_same_sentence():
    """The three keys still promise the outcome, now with the way to see it."""
    for key in PROMISING_KEYS:
        for locale in ("en", "ca", "es"):
            assert translate(key, locale) != key
            assert len(translate(key, locale).split()) > 5, (key, locale)


def test_a_finished_scan_offers_no_polling_script(tmp_path, login):
    app, runner = _app(tmp_path, lambda: {})
    client = app.test_client()
    assert login(client).status_code == 303
    assert runner.run("manual").status == "finished"

    html = client.get("/?lang=en").get_data(as_text=True)
    assert 'id="scan-status"' not in html
    assert "js/scan-status.js" not in html
