from __future__ import annotations

import json

from library_curator.config import load_settings


def test_fresh_install_defaults_start_in_dry_run(settings):
    assert settings.dry_run is True
    assert settings.grace_days == 14
    assert settings.collection_name == "Leaving soon"
    assert settings.minimum_rating == 8.0
    assert settings.database_path == settings.config_dir / "library-curator.db"


def test_load_settings_persists_defaults_in_the_config_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_CONFIG_DIR", str(tmp_path / "config"))

    loaded = load_settings()

    assert loaded.config_dir == tmp_path / "config"
    saved = json.loads((tmp_path / "config" / "settings.json").read_text())
    assert saved["dry_run"] is True
    assert saved["grace_days"] == 14


def test_environment_overrides_the_saved_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LCP_DRY_RUN", "0")

    loaded = load_settings()

    assert loaded.dry_run is False


def test_threshold_defaults_match_the_requirements(settings):
    thresholds = settings.thresholds

    assert thresholds.stalled_series_months == 6
    assert thresholds.never_watched_months == 3
    assert thresholds.cancelled_no_viewers_months == 6
    assert thresholds.everyone_finished_months == 1
    assert thresholds.movies_not_rewatched_months == 18
