"""Application settings.

Settings live in a JSON file inside the config directory (``/config`` in the
container) and can be overridden per run by environment variables prefixed
with ``ALC_``. A fresh install starts in dry-run mode and never deletes media
until the owner turns real deletion on.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from almali_library_curator import secret_store

ENV_PREFIX = "ALC_"
DEFAULT_CONFIG_DIR = "/config"
SETTINGS_FILE_NAME = "settings.json"
DATABASE_FILE_NAME = "almali-library-curator.db"

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Credentials: never written to the settings file, never logged (S2). Each
# resolves as its non-blank ``ALC_`` variable > the encrypted ``secret_store``
# entry (S12 for the Plex token, S20 for the service keys) > empty.
SECRET_FIELDS = frozenset(
    {
        "plex_token",
        "sonarr_api_key",
        "radarr_api_key",
        "tautulli_api_key",
        "tmdb_api_token",
    }
)

# Resolved per run from the environment and the secrets store; never read
# from or written to the settings file.
RUNTIME_FIELDS = frozenset(
    {"plex_token_status", "plex_env_managed", "stored_secrets", "unreadable_secrets",
     "env_managed_services"}
)

# S20: the optional services whose keys are entered in the app, with their
# key field and the environment fields that, while set, manage the service.
SERVICE_KEY_FIELDS = {
    "sonarr": "sonarr_api_key",
    "radarr": "radarr_api_key",
    "tautulli": "tautulli_api_key",
    "tmdb": "tmdb_api_token",
}
SERVICE_ENVIRONMENT_FIELDS = {
    "sonarr": ("sonarr_base_url", "sonarr_api_key"),
    "radarr": ("radarr_base_url", "radarr_api_key"),
    "tautulli": ("tautulli_base_url", "tautulli_api_key"),
    "tmdb": ("tmdb_api_token",),
}

# Plex token origin, as ``Settings.plex_token_status`` reports it.
PLEX_TOKEN_NONE = "none"
PLEX_TOKEN_ENVIRONMENT = "environment"
PLEX_TOKEN_STORED = "stored"
PLEX_TOKEN_UNREADABLE = "unreadable"

# The environment variables that, while set, manage Plex instead of the app.
PLEX_ENVIRONMENT_FIELDS = ("plex_base_url", "plex_token")


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
    curated_libraries: tuple[str, ...] = ()
    excluded_users: tuple[str, ...] = ()
    scan_time: str = "06:00"
    scheduler_enabled: bool = True
    plex_base_url: str = ""
    plex_token: str = field(default="", repr=False)
    plex_token_status: str = PLEX_TOKEN_NONE
    plex_env_managed: bool = False
    plex_timeout_seconds: float = 15.0
    plex_rate_limit_per_second: float = 5.0
    plex_page_size: int = 200
    plex_path_map: tuple[tuple[str, str], ...] = ()
    sonarr_base_url: str = ""
    sonarr_api_key: str = field(default="", repr=False)
    radarr_base_url: str = ""
    radarr_api_key: str = field(default="", repr=False)
    # S15 rule 2: a configured arr removes its own unit type unless its
    # switch is off; its status and linking jobs keep working either way.
    radarr_deletes: bool = True
    sonarr_deletes: bool = True
    tautulli_base_url: str = ""
    tautulli_api_key: str = field(default="", repr=False)
    tmdb_base_url: str = ""
    tmdb_api_token: str = field(default="", repr=False)
    # S20 runtime facts: which service keys came from the encrypted store,
    # which stored entries could not be read, which services the
    # environment manages.
    stored_secrets: tuple[str, ...] = ()
    unreadable_secrets: tuple[str, ...] = ()
    env_managed_services: tuple[str, ...] = ()
    media_root: str = "/media"
    unraid_root: str = ""
    thresholds: Thresholds = field(default_factory=Thresholds)

    def __post_init__(self) -> None:
        if not isinstance(self.scan_time, str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.scan_time
        ):
            raise ValueError("scan_time must be a local time in HH:MM format")
        object.__setattr__(self, "config_dir", Path(self.config_dir))
        database_path = (
            self.config_dir / DATABASE_FILE_NAME
            if self.database_path is None
            else Path(self.database_path)
        )
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "protected_libraries", tuple(self.protected_libraries))
        object.__setattr__(self, "curated_libraries", tuple(self.curated_libraries))
        object.__setattr__(self, "excluded_users", tuple(self.excluded_users))
        for name in ("stored_secrets", "unreadable_secrets", "env_managed_services"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
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
    "SCAN_TIME": ("scan_time", str),
    "SCHEDULER_ENABLED": ("scheduler_enabled", bool),
    "PROTECTED_LIBRARIES": ("protected_libraries", list),
    "CURATED_LIBRARIES": ("curated_libraries", list),
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
    "RADARR_DELETES": ("radarr_deletes", bool),
    "SONARR_DELETES": ("sonarr_deletes", bool),
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
    """Atomically replace the settings file with owner-only permissions."""
    with secret_store.writer_lock(path.parent):
        _write_settings_changes(path, values, replace=True)


def update_settings_file(config_dir: str | Path, changes: Mapping[str, Any]) -> Path:
    """Change only the named non-secret keys of the settings file, atomically.

    Unlike :func:`save_settings`, values resolved from the environment are
    never written back: every other key keeps its stored value.
    """
    refused = (SECRET_FIELDS | RUNTIME_FIELDS | {"config_dir", "database_path"}) & set(changes)
    if refused:
        raise ValueError("secret or runtime fields cannot be written to the settings file")
    path = Path(config_dir) / SETTINGS_FILE_NAME
    with secret_store.writer_lock(path.parent):
        return _write_settings_changes(path, changes)


def _write_settings_changes(
    path: Path, changes: Mapping[str, Any], *, replace: bool = False
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = dict(changes) if replace else _read_settings_file(path)
    if not replace:
        values.update(changes)
    # A symlinked settings file keeps its link: the new content replaces the
    # resolved target, not the link itself.
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{SETTINGS_FILE_NAME}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(values, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        os.unlink(temporary)
        raise
    return path


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
    for name in SECRET_FIELDS | RUNTIME_FIELDS:
        values.pop(name, None)
    return values


def load_settings(
    config_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from disk and environment.

    The settings file is created with the defaults on first run. Environment
    variables win over the file and are never written back to it.

    The Plex token resolves as ``ALC_PLEX_TOKEN`` > the encrypted store >
    empty. An unreadable store never raises: the token is empty and
    ``plex_token_status`` is ``unreadable`` (S12 rule 9). A blank
    ``ALC_PLEX_BASE_URL`` or ``ALC_PLEX_TOKEN`` counts as unset.
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
        if name not in {"config_dir", "database_path"} | SECRET_FIELDS | RUNTIME_FIELDS
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

    overrides = _env_overrides(environment)
    # A blank Plex or service credential variable counts as unset (S12, S20).
    blank_unset = set(PLEX_ENVIRONMENT_FIELDS).union(*SERVICE_ENVIRONMENT_FIELDS.values())
    for name in blank_unset:
        if name in overrides and not str(overrides[name]).strip():
            del overrides[name]
    # S14 rule 1: an explicit empty ALC_CURATED_LIBRARIES means the same
    # as unset, so a stored selection keeps applying.
    if not overrides.get("curated_libraries"):
        overrides.pop("curated_libraries", None)
    resolved = dict(stored)
    resolved.update(overrides)
    resolved.pop("thresholds", None)

    resolved["plex_env_managed"] = any(name in overrides for name in PLEX_ENVIRONMENT_FIELDS)
    resolved["env_managed_services"] = tuple(
        service for service, names in SERVICE_ENVIRONMENT_FIELDS.items()
        if any(name in overrides for name in names)
    )
    from_store = [name for name in SECRET_FIELDS if name not in overrides]
    states = secret_store.read_secrets(directory, from_store) if from_store else {}
    stored_secrets, unreadable_secrets = [], []
    for name in sorted(SERVICE_KEY_FIELDS.values()):
        if name not in states:
            continue
        resolved[name] = states[name].token
        if states[name].status == secret_store.STORED:
            stored_secrets.append(name)
        elif states[name].status == secret_store.UNREADABLE:
            unreadable_secrets.append(name)
    resolved["stored_secrets"] = tuple(stored_secrets)
    resolved["unreadable_secrets"] = tuple(unreadable_secrets)
    if "plex_token" in overrides:
        resolved["plex_token_status"] = PLEX_TOKEN_ENVIRONMENT
    else:
        state = states[secret_store.PLEX_TOKEN]
        resolved["plex_token"] = state.token
        resolved["plex_token_status"] = {
            secret_store.STORED: PLEX_TOKEN_STORED,
            secret_store.UNREADABLE: PLEX_TOKEN_UNREADABLE,
        }.get(state.status, PLEX_TOKEN_NONE)

    if isinstance(resolved.get("protected_libraries"), list):
        resolved["protected_libraries"] = tuple(resolved["protected_libraries"])
    if isinstance(resolved.get("curated_libraries"), list):
        resolved["curated_libraries"] = tuple(resolved["curated_libraries"])
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
