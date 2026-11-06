from __future__ import annotations

import json

import pytest

from almali_library_curator.config import ENV_PREFIX, load_settings


def test_scan_schedule_defaults_legacy_and_environment(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"scan_interval_hours": 4}))
    loaded = load_settings(tmp_path, env={})
    assert loaded.scan_time == "06:00"
    assert loaded.scheduler_enabled is True
    assert not hasattr(loaded, "scan_interval_hours")
    loaded = load_settings(tmp_path, env={"ALC_SCAN_TIME": "23:05", "ALC_SCHEDULER_ENABLED": "false"})
    assert loaded.scan_time == "23:05"
    assert loaded.scheduler_enabled is False


@pytest.mark.parametrize("value", ["6:00", "24:00", "06:60", "06:00:00", "aa:bb", "06:00\\n"])
def test_scan_time_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        load_settings(tmp_path, env={"ALC_SCAN_TIME": value})


# -- S14: the curated-library scope ------------------------------------------


def test_curated_libraries_default_to_empty_which_means_every_library(settings):
    assert settings.curated_libraries == ()


def test_curated_libraries_round_trip_through_the_settings_file(tmp_path):
    from almali_library_curator.config import save_settings

    loaded = load_settings(tmp_path, env={"ALC_CURATED_LIBRARIES": "1, 2"})
    assert loaded.curated_libraries == ("1", "2")
    save_settings(loaded)

    reread = load_settings(tmp_path, env={})
    assert reread.curated_libraries == ("1", "2")
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["curated_libraries"] == ["1", "2"]


def test_the_curated_libraries_environment_overrides_the_file(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"curated_libraries": ["1"]}))

    loaded = load_settings(tmp_path, env={"ALC_CURATED_LIBRARIES": "3,4"})

    assert loaded.curated_libraries == ("3", "4")


def test_an_empty_curated_libraries_environment_value_means_the_same_as_unset(tmp_path):
    # S14 rule 1: an explicit empty environment value is ignored, so a
    # stored selection keeps applying exactly as with the variable unset.
    (tmp_path / "settings.json").write_text(json.dumps({"curated_libraries": ["1"]}))

    assert load_settings(tmp_path, env={"ALC_CURATED_LIBRARIES": ""}).curated_libraries == ("1",)
    assert load_settings(tmp_path, env={}).curated_libraries == ("1",)


def test_an_install_upgraded_from_v0_10_0_keeps_curating_every_library(tmp_path):
    # A v0.10.0 settings.json has no curated_libraries key at all.
    (tmp_path / "settings.json").write_text(json.dumps({"grace_days": 21, "dry_run": False}))

    loaded = load_settings(tmp_path, env={})

    assert loaded.curated_libraries == ()


def test_update_settings_file_round_trips_the_curated_selection(tmp_path):
    from almali_library_curator.config import update_settings_file

    load_settings(tmp_path, env={})
    update_settings_file(tmp_path, {"curated_libraries": ["2"]})

    assert load_settings(tmp_path, env={}).curated_libraries == ("2",)


def test_the_scope_rule_treats_an_empty_selection_as_every_library(settings):
    from almali_library_curator import library_scope

    assert library_scope.curated_library_keys(settings) == ()
    assert library_scope.library_is_curated("1", ())
    assert library_scope.library_is_curated("1", ("1",))
    assert not library_scope.library_is_curated("2", ("1",))


def test_the_environment_prefix_is_the_almali_prefix():
    assert ENV_PREFIX == "ALC_"


def test_the_former_environment_prefix_is_no_longer_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("ALC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LCP_DRY_RUN", "0")

    loaded = load_settings()

    assert loaded.dry_run is True


def test_fresh_install_defaults_start_in_dry_run(settings):
    assert settings.dry_run is True
    assert settings.grace_days == 14
    assert settings.collection_name == "Leaving soon"
    assert settings.minimum_rating == 8.0
    assert settings.database_path == settings.config_dir / "almali-library-curator.db"


def test_load_settings_persists_defaults_in_the_config_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ALC_CONFIG_DIR", str(tmp_path / "config"))

    loaded = load_settings()

    assert loaded.config_dir == tmp_path / "config"
    saved = json.loads((tmp_path / "config" / "settings.json").read_text())
    assert saved["dry_run"] is True
    assert saved["grace_days"] == 14


def test_environment_overrides_the_saved_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ALC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALC_DRY_RUN", "0")

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
    monkeypatch.setenv("ALC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALC_SONARR_BASE_URL", "http://sonarr:8989")
    monkeypatch.setenv("ALC_SONARR_API_KEY", "sonarr-secret")
    monkeypatch.setenv("ALC_RADARR_API_KEY", "radarr-secret")
    monkeypatch.setenv("ALC_TAUTULLI_API_KEY", "tautulli-secret")
    monkeypatch.setenv("ALC_TMDB_API_TOKEN", "tmdb-secret")

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


# -- S12: the Plex token resolves environment > encrypted store > empty --------

PLEX_TOKEN = "stored-plex-token-ab12"


def test_load_settings_returns_the_stored_plex_token_and_never_persists_it(tmp_path):
    from almali_library_curator import secret_store

    secret_store.store_plex_token(tmp_path, PLEX_TOKEN)
    loaded = load_settings(tmp_path, env={})
    assert loaded.plex_token == PLEX_TOKEN
    assert loaded.plex_token_status == "stored" and loaded.plex_env_managed is False
    assert PLEX_TOKEN not in repr(loaded)
    saved = (tmp_path / "settings.json").read_text()
    assert PLEX_TOKEN not in saved
    assert "plex_token" not in json.loads(saved)
    assert "plex_token_status" not in json.loads(saved) and "plex_env_managed" not in json.loads(saved)


def test_the_environment_token_wins_over_the_stored_token(tmp_path):
    from almali_library_curator import secret_store

    secret_store.store_plex_token(tmp_path, PLEX_TOKEN)
    loaded = load_settings(tmp_path, env={"ALC_PLEX_TOKEN": "env-token"})
    assert loaded.plex_token == "env-token"
    assert loaded.plex_token_status == "environment" and loaded.plex_env_managed is True


def test_the_environment_address_marks_plex_as_environment_managed(tmp_path):
    loaded = load_settings(tmp_path, env={"ALC_PLEX_BASE_URL": "http://plex:32400"})
    assert loaded.plex_base_url == "http://plex:32400" and loaded.plex_env_managed is True


def test_blank_plex_environment_variables_do_not_override(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"plex_base_url": "http://saved:32400"}))
    loaded = load_settings(tmp_path, env={"ALC_PLEX_BASE_URL": "", "ALC_PLEX_TOKEN": "  "})
    assert loaded.plex_base_url == "http://saved:32400"
    assert loaded.plex_env_managed is False and loaded.plex_token == ""


def test_no_token_anywhere_resolves_empty(tmp_path):
    loaded = load_settings(tmp_path, env={})
    assert loaded.plex_token == "" and loaded.plex_token_status == "none"


def test_a_settings_file_cannot_inject_the_token_or_its_status(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"plex_token": "file-token", "plex_token_status": "stored", "plex_env_managed": True}))
    loaded = load_settings(tmp_path, env={})
    assert loaded.plex_token == "" and loaded.plex_token_status == "none"
    assert loaded.plex_env_managed is False


@pytest.mark.parametrize("damage", ["corrupt_store", "missing_key", "wrong_key"])
def test_unreadable_secrets_never_stop_loading(tmp_path, caplog, damage):
    import logging

    from cryptography.fernet import Fernet

    from almali_library_curator import secret_store

    secret_store.store_plex_token(tmp_path, PLEX_TOKEN)
    if damage == "corrupt_store":
        (tmp_path / "secrets.json").write_bytes(b"\x00garbage")
    elif damage == "missing_key":
        (tmp_path / "secrets.key").unlink()
    else:
        (tmp_path / "secrets.key").write_bytes(Fernet.generate_key())
    caplog.set_level(logging.INFO)
    loaded = load_settings(tmp_path, env={})
    assert loaded.plex_token == "" and loaded.plex_token_status == "unreadable"
    assert any(record.getMessage() == "secret_store_unreadable" for record in caplog.records)
    assert PLEX_TOKEN not in caplog.text


def test_update_settings_file_changes_only_the_named_key(tmp_path):
    from almali_library_curator.config import update_settings_file

    loaded = load_settings(tmp_path, env={"ALC_DRY_RUN": "0"})
    assert loaded.dry_run is False
    update_settings_file(tmp_path, {"plex_base_url": "http://plex:32400"})
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["plex_base_url"] == "http://plex:32400"
    assert saved["dry_run"] is True  # the environment is never written back
    with pytest.raises(ValueError):
        update_settings_file(tmp_path, {"plex_token": "sentinel-token-x"})
    assert "sentinel-token-x" not in (tmp_path / "settings.json").read_text()


# -- S15 rule 2: the two deletion switches ------------------------------------


def test_both_arr_deletion_switches_default_to_on(tmp_path):
    loaded = load_settings(tmp_path, env={})
    assert (loaded.radarr_deletes, loaded.sonarr_deletes) == (True, True)
    stored = json.loads((tmp_path / "settings.json").read_text())
    assert (stored["radarr_deletes"], stored["sonarr_deletes"]) == (True, True)


def test_the_deletion_switches_round_trip_and_the_environment_wins(tmp_path):
    from almali_library_curator.config import update_settings_file

    update_settings_file(tmp_path, {"radarr_deletes": False})
    loaded = load_settings(tmp_path, env={})
    assert (loaded.radarr_deletes, loaded.sonarr_deletes) == (False, True)
    loaded = load_settings(tmp_path, env={"ALC_RADARR_DELETES": "true",
                                          "ALC_SONARR_DELETES": "false"})
    assert (loaded.radarr_deletes, loaded.sonarr_deletes) == (True, False)
    assert json.loads((tmp_path / "settings.json").read_text())["radarr_deletes"] is False
