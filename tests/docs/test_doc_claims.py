"""Documented claims that must stay true against the code (#66)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from almali_library_curator.app import create_app
from almali_library_curator.logging_config import configure_logging

ROOT = Path(__file__).resolve().parents[2]


def _config_row(text: str) -> str:
    rows = [line for line in text.splitlines() if re.match(r"\|\s*`/config`\s*\|", line)]
    assert rows, "no /config row in the volume table"
    return "\n".join(rows)


def test_config_volume_rows_do_not_promise_log_files():
    # The app logs to the container output only; nothing writes log files,
    # so the /config row may only say where the logs go instead.
    for doc in ("README.md", "docs/UNRAID.md"):
        row = _config_row((ROOT / doc).read_text(encoding="utf-8"))
        assert "logs go to" in row and "logs," not in row, doc
    unraid = (ROOT / "docs/UNRAID.md").read_text(encoding="utf-8")
    app_data = [line for line in unraid.splitlines() if line.startswith("| App data directory")]
    assert app_data and "logs" not in app_data[0]


def test_logging_goes_to_a_stream_not_a_file():
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        # pytest installs its own handlers on the root logger; judge only
        # the ones the app adds.
        root.handlers[:] = []
        configure_logging()
        assert root.handlers
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
    finally:
        root.handlers[:] = saved


def test_issue_forms_do_not_send_reporters_to_healthz_for_the_version(settings):
    payload = create_app(settings).test_client().get("/healthz").get_json()
    assert "version" not in payload
    for form in sorted((ROOT / ".github/ISSUE_TEMPLATE").glob("*.yml")):
        text = form.read_text(encoding="utf-8")
        assert "`/healthz` reports it" not in text, form.name
        assert "v0.15.0" not in text, form.name


def test_security_policy_states_every_encrypted_secret_and_the_env_exception():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for name in ("Sonarr", "Radarr", "Tautulli", "TMDB"):
        assert name in text
    assert "environment variables are not encrypted" in text
    assert "last four characters" in text
    assert "`missing`" in text


def test_contributing_names_no_cli_entry_point_the_package_lacks():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" not in pyproject
    assert "CLI entry point" not in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
