"""Application settings.

Settings live in a JSON file inside the config directory (``/config`` in the
container) and can be overridden per run by environment variables prefixed
with ``LCP_``. A fresh install starts in dry-run mode and never deletes media
until the owner turns real deletion on.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

ENV_PREFIX = "LCP_"
DEFAULT_CONFIG_DIR = "/config"
SETTINGS_FILE_NAME = "settings.json"
DATABASE_FILE_NAME = "library-curator.db"

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Credentials that live only in the environment: never written to the
# settings file, never logged (S2). Encryption at rest arrives with T11.
SECRET_FIELDS = frozenset(
    {
        "plex_token",
        "sonarr_api_key",
        "radarr_api_key",
        "tautulli_api_key",
        "tmdb_api_token",
    }
)


@dataclass(frozen=True)
class Thresholds:
    """Detection thresholds, in months. Global settings in v1."""

    stalled_series_months: int = 6
    never_watched_months: int = 3
    cancelled_no_viewers_months: int = 6
    everyone_finished_months: int = 1
    movies_not_rewatched_months: int = 18


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one application run."""

    config_dir: Path
    database_path: Path | None = None
    log_level: str = "INFO"
    dry_run: bool = True
    locale: str = "en"
    grace_days: int = 14
    collection_name: str = "Leaving soon"
    minimum_rating: float = 8.0
    rating_protection_enabled: bool = True
    protected_libraries: tuple[str, ...] = ()
    excluded_users: tuple[str, ...] = ()
    scan_interval_hours: int = 24
    plex_base_url: str = ""
    plex_token: str = ""
    plex_timeout_seconds: float = 15.0
    plex_rate_limit_per_second: float = 5.0
    plex_page_size: int = 200
    plex_path_map: tuple[tuple[str, str], ...] = ()
    sonarr_base_url: str = ""
    sonarr_api_key: str = ""
    radarr_base_url: str = ""
    radarr_api_key: str = ""
    tautulli_base_url: str = ""
    tautulli_api_key: str = ""
    tmdb_base_url: str = ""
    tmdb_api_token: str = ""
    media_root: str = "/media"
    unraid_root: str = ""
    thresholds: Thresholds = field(default_factory=Thresholds)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_dir", Path(self.config_dir))
        database_path = (
            self.config_dir / DATABASE_FILE_NAME
            if self.database_path is None
            else Path(self.database_path)
        )
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "protected_libraries", tuple(self.protected_libraries))
        object.__setattr__(self, "excluded_users", tuple(self.excluded_users))
        object.__setattr__(
            self,
            "plex_path_map",
            tuple((str(source), str(target)) for source, target in self.plex_path_map),
        )


_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "DATABASE_PATH": ("database_path", str),
    "LOG_LEVEL": ("log_level", str),
    "DRY_RUN": ("dry_run", bool),
    "LOCALE": ("locale", str),
    "GRACE_DAYS": ("grace_days", int),
    "COLLECTION_NAME": ("collection_name", str),
    "MINIMUM_RATING": ("minimum_rating", float),
    "SCAN_INTERVAL_HOURS": ("scan_interval_hours", int),
    "PROTECTED_LIBRARIES": ("protected_libraries", list),
    "EXCLUDED_USERS": ("excluded_users", list),
    "PLEX_BASE_URL": ("plex_base_url", str),
    "PLEX_TOKEN": ("plex_token", str),
    "PLEX_TIMEOUT_SECONDS": ("plex_timeout_seconds", float),
    "PLEX_RATE_LIMIT_PER_SECOND": ("plex_rate_limit_per_second", float),
    "PLEX_PAGE_SIZE": ("plex_page_size", int),
    "PLEX_PATH_MAP": ("plex_path_map", list),
    "SONARR_BASE_URL": ("sonarr_base_url", str),
    "SONARR_API_KEY": ("sonarr_api_key", str),
    "RADARR_BASE_URL": ("radarr_base_url", str),
    "RADARR_API_KEY": ("radarr_api_key", str),
    "TAUTULLI_BASE_URL": ("tautulli_base_url", str),
    "TAUTULLI_API_KEY": ("tautulli_api_key", str),
    "TMDB_BASE_URL": ("tmdb_base_url", str),
    "TMDB_API_TOKEN": ("tmdb_api_token", str),
    "MEDIA_ROOT": ("media_root", str),
    "UNRAID_ROOT": ("unraid_root", str),
}


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_VALUES


def _read_settings_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return content if isinstance(content, dict) else {}


def _write_settings_file(path: Path, values: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for suffix, (name, kind) in _ENV_OVERRIDES.items():
        raw = env.get(f"{ENV_PREFIX}{suffix}")
        if raw is None:
            continue
        if kind is bool:
            overrides[name] = _parse_bool(raw)
        elif kind is int:
            overrides[name] = int(raw)
        elif kind is float:
            overrides[name] = float(raw)
        elif kind is list:
            overrides[name] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            overrides[name] = raw
    return overrides


def _serializable(settings: Settings) -> dict[str, Any]:
    values = asdict(settings)
    values.pop("config_dir", None)
    values.pop("database_path", None)
    for name in SECRET_FIELDS:
        values.pop(name, None)
    return values


def load_settings(
    config_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from disk and environment.

    The settings file is created with the defaults on first run. Environment
    variables win over the file and are never written back to it.
    """
    environment = os.environ if env is None else env
    directory = Path(
        config_dir
        or environment.get(f"{ENV_PREFIX}CONFIG_DIR")
        or DEFAULT_CONFIG_DIR
    )
    directory.mkdir(parents=True, exist_ok=True)

    settings_path = directory / SETTINGS_FILE_NAME
    saved = _read_settings_file(settings_path)

    base = Settings(config_dir=directory)
    stored: dict[str, Any] = {
        name: getattr(base, name)
        for name in (field_.name for field_ in fields(Settings))
        if name not in {"config_dir", "database_path"} | SECRET_FIELDS
    }
    stored.update({key: value for key, value in saved.items() if key in stored})

    threshold_values = asdict(Thresholds())
    raw_thresholds = stored.pop("thresholds", None)
    if isinstance(raw_thresholds, Thresholds):
        threshold_values.update(asdict(raw_thresholds))
    elif isinstance(raw_thresholds, Mapping):
        threshold_values.update(
            {key: value for key, value in raw_thresholds.items() if key in threshold_values}
        )
    stored["thresholds"] = threshold_values

    if not settings_path.exists():
        _write_settings_file(settings_path, stored)

    resolved = dict(stored)
    resolved.update(_env_overrides(environment))
    resolved.pop("thresholds", None)

    if isinstance(resolved.get("protected_libraries"), list):
        resolved["protected_libraries"] = tuple(resolved["protected_libraries"])
    if isinstance(resolved.get("excluded_users"), list):
        resolved["excluded_users"] = tuple(resolved["excluded_users"])
    if resolved.get("plex_path_map"):
        pairs: list[tuple[str, str]] = []
        for entry in resolved["plex_path_map"]:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((str(entry[0]).strip(), str(entry[1]).strip()))
            elif isinstance(entry, str) and "=" in entry:
                source, _, target = entry.partition("=")
                pairs.append((source.strip(), target.strip()))
        resolved["plex_path_map"] = tuple(pairs)

    return Settings(config_dir=directory, **resolved)


def save_settings(settings: Settings) -> Path:
    """Write the settings back to the settings file and return its path."""
    path = settings.config_dir / SETTINGS_FILE_NAME
    _write_settings_file(path, _serializable(settings))
    return path
