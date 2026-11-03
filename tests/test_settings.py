from __future__ import annotations

import json

from almali_library_curator.config import load_settings


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


def test_optional_service_settings_default_to_unconfigured(settings):
    assert settings.sonarr_base_url == ""
    assert settings.sonarr_api_key == ""
    assert settings.radarr_base_url == ""
    assert settings.radarr_api_key == ""
    assert settings.tautulli_base_url == ""
    assert settings.tautulli_api_key == ""
    assert settings.tmdb_base_url == ""
    assert settings.tmdb_api_token == ""
    assert settings.media_root == "/media"
    assert settings.unraid_root == ""


def test_service_secrets_come_from_the_environment_and_never_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LCP_SONARR_BASE_URL", "http://sonarr:8989")
    monkeypatch.setenv("LCP_SONARR_API_KEY", "sonarr-secret")
    monkeypatch.setenv("LCP_RADARR_API_KEY", "radarr-secret")
    monkeypatch.setenv("LCP_TAUTULLI_API_KEY", "tautulli-secret")
    monkeypatch.setenv("LCP_TMDB_API_TOKEN", "tmdb-secret")

    loaded = load_settings()
    reread = load_settings()

    assert loaded.sonarr_base_url == "http://sonarr:8989"
    assert loaded.sonarr_api_key == "sonarr-secret"
    assert loaded.radarr_api_key == "radarr-secret"
    assert loaded.tautulli_api_key == "tautulli-secret"
    assert loaded.tmdb_api_token == "tmdb-secret"
    assert reread.sonarr_base_url == "http://sonarr:8989"
    saved = json.loads((tmp_path / "config" / "settings.json").read_text())
    for secret in ("sonarr-secret", "radarr-secret", "tautulli-secret", "tmdb-secret"):
        assert secret not in saved
    for field in ("plex_token", "sonarr_api_key", "radarr_api_key", "tautulli_api_key", "tmdb_api_token"):
        assert field not in saved
