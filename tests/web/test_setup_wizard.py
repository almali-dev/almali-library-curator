"""The wizard is a real step by step: no dead ends (S2).

The owner reached Libraries with Plex connected and an analysis already run
and failed, and the step answered an empty list plus "connect Plex and run
an analysis". Every step must say what is done, what is missing and what to
press next, using the recorded scan run for its facts.
"""
from __future__ import annotations

import pytest

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings, save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.scan import ScanRunner


class ValidationError(Exception):
    """The same diagnostic the owner's real run recorded."""


def _failing_steps(*args):
    def sync():
        raise ValidationError("part")
    return {"sync": sync, **{name: (lambda: {}) for name in
                             ("enrichment", "detection", "recheck", "deletion")}}


@pytest.fixture
def failed_scan(tmp_path):
    """A real app whose first recorded analysis failed in the sync step."""
    settings = Settings(config_dir=tmp_path / "config", scheduler_enabled=False)
    save_settings(settings)
    runner = ScanRunner(settings, steps_factory=_failing_steps)
    app = create_app(settings, scan_runner=runner)
    app.config.update(TESTING=True, SECRET_KEY="web-test-session", WTF_CSRF_ENABLED=False)
    assert runner.run("manual").status == "failed"
    return app.test_client()


@pytest.fixture
def client(app):
    return app.test_client()


def test_libraries_step_explains_a_failed_scan_and_offers_the_retry(failed_scan, login):
    assert login(failed_scan).status_code == 303
    html = failed_scan.get("/setup?step=libraries&lang=en").get_data(as_text=True)
    # What is missing, in the owner's words, and how to prepare the step.
    assert translate("setup.libraries_retry", "en") in html
    assert translate("scan.status.failed", "en") in html
    assert "ValidationError" not in html
    # The dead-end copy is gone, and the step offers the action that fixes it.
    assert translate("setup.no_libraries", "en") not in html
    assert translate("action.scan", "en") in html.split('class="card wizard-step"', 1)[1].split("</section>", 1)[0]


def test_libraries_step_offers_the_first_analysis_when_none_ran(client, login):
    html = client.get("/setup?step=libraries&lang=en").get_data(as_text=True)
    assert translate("setup.libraries_run_scan", "en") in html
    assert translate("setup.no_libraries", "en") not in html
    assert translate("action.scan", "en") in html


def test_libraries_step_says_what_is_done_once_the_analysis_found_libraries(client, conn, login):
    conn.execute("INSERT INTO libraries (plex_key, title, section_type) VALUES ('1', 'Movies', 'movie')")
    conn.commit()
    html = client.get("/setup?step=libraries&lang=en").get_data(as_text=True)
    assert translate("setup.libraries_done", "en") in html
    assert "Movies" in html
    assert translate("setup.no_libraries", "en") not in html


def test_plex_step_says_it_is_already_done(client, monkeypatch):
    monkeypatch.setenv("ALC_PLEX_BASE_URL", "http://plex.invalid")
    monkeypatch.setenv("ALC_PLEX_TOKEN", "token")
    html = client.get("/setup?step=plex&lang=en").get_data(as_text=True)
    assert translate("setup.plex_done", "en") in html
