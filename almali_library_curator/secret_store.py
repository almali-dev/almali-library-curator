"""Encrypted, owner-only storage of the Plex token and the service keys.

The Plex token (S12) and the Sonarr, Radarr, Tautulli and TMDB keys (S20)
are encrypted with Fernet (``cryptography``) and written to
``<config_dir>/secrets.json``, one entry each; the key lives in
``<config_dir>/secrets.key``.
Both files are mode 0600. This protects a leaked settings file, backup or
screenshot, not a fully compromised config directory (L70).

Concurrency: the key is published with ``os.link`` from a fully written
private temporary file, so concurrent Gunicorn workers agree on one key and
nobody reads a half-written key. Writers serialise on an owner-only lock file
and replace ``secrets.json`` atomically with ``os.replace``.

A missing, corrupted or undecryptable store, or a missing or wrong key, never
raises on read: the entry reads as empty with status ``unreadable`` and a
structured log event that carries the entry name and the failure reason,
never a secret. A key file without a store is a lost Plex token only: the
optional service keys then read as absent (S20 rule 9).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import threading

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEY_FILE_NAME = "secrets.key"
STORE_FILE_NAME = "secrets.json"
LOCK_FILE_NAME = ".secrets.lock"
STORE_VERSION = 1
PLEX_TOKEN = "plex_token"
# Every entry the store may hold (S12, S20).
SECRET_NAMES = frozenset(
    {PLEX_TOKEN, "sonarr_api_key", "radarr_api_key", "tautulli_api_key", "tmdb_api_token"}
)

ABSENT = "absent"
STORED = "stored"
UNREADABLE = "unreadable"

MASK = "••••"

_reported: set[tuple] = set()
_reported_lock = threading.Lock()


@dataclass(frozen=True)
class TokenState:
    """One stored entry and how it was read. The value never reaches repr()."""

    token: str = field(repr=False)
    status: str


def mask(token: str) -> str:
    """Show that a token is stored with at most its last four characters."""
    return MASK + token[-4:] if len(token) >= 8 else MASK


# -- files --------------------------------------------------------------------

def _private_temporary(directory: Path, name: str, data: bytes) -> str:
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
    except BaseException:
        os.unlink(temporary)
        raise
    return temporary


def _publish_once(path: Path, data: bytes) -> None:
    """Create ``path`` with ``data`` unless another writer published first."""
    temporary = _private_temporary(path.parent, path.name, data)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


def _replace(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with an owner-only file holding ``data``."""
    temporary = _private_temporary(path.parent, path.name, data)
    try:
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


_dir_locks: dict[Path, threading.RLock] = {}
_dir_locks_guard = threading.Lock()
_lock_depth = threading.local()


@contextmanager
def writer_lock(config_dir: str | Path):
    """Reentrant per-thread and cross-process writer lock."""
    directory = Path(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    with _dir_locks_guard:
        tlock = _dir_locks.setdefault(resolved, threading.RLock())
    depths = getattr(_lock_depth, "counts", None)
    if depths is None:
        depths = _lock_depth.counts = {}
    with tlock:
        depth = depths.get(resolved, 0)
        fd = None
        if depth == 0:
            fd = os.open(directory / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
        depths[resolved] = depth + 1
        try:
            yield
        finally:
            depths[resolved] = depth
            if depth == 0:
                depths.pop(resolved, None)
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)


_writer_lock = writer_lock


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def snapshot_store(config_dir: str | Path) -> tuple[bytes | None, bytes | None]:
    """Raw bytes of the store and key files (callers hold writer_lock)."""
    directory = Path(config_dir)
    return (_read_optional(directory / STORE_FILE_NAME), _read_optional(directory / KEY_FILE_NAME))


def restore_store(config_dir: str | Path, snapshot: tuple[bytes | None, bytes | None]) -> None:
    """Restore the store and key files from a snapshot (callers hold writer_lock)."""
    directory = Path(config_dir)
    for name, prior in ((STORE_FILE_NAME, snapshot[0]), (KEY_FILE_NAME, snapshot[1])):
        path = directory / name
        if prior is None:
            path.unlink(missing_ok=True)
        elif _read_optional(path) != prior:
            _replace(path, prior)


def ensure_key(config_dir: str | Path) -> bytes:
    """The installation's Fernet key, created once across concurrent workers."""
    path = Path(config_dir) / KEY_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _publish_once(path, Fernet.generate_key())
    return path.read_bytes()


def _usable_key(directory: Path) -> bytes:
    """A valid key; a damaged key file is replaced (callers hold the lock)."""
    key = ensure_key(directory).strip()
    try:
        Fernet(key)
        return key
    except (ValueError, TypeError):
        logger.warning("secret_key_replaced", extra={"reason": "key_invalid"})
        key = Fernet.generate_key()
        _replace(directory / KEY_FILE_NAME, key)
        return key


def _payload_for_update(directory: Path) -> dict:
    try:
        payload = json.loads((directory / STORE_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_payload(directory: Path, payload: dict) -> None:
    payload["version"] = STORE_VERSION
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _replace(directory / STORE_FILE_NAME, data)


# -- public API ---------------------------------------------------------------

def _checked(name: str) -> str:
    if name not in SECRET_NAMES:
        raise ValueError("unknown secret entry")
    return name


def store_secret(config_dir: str | Path, name: str, value: str) -> None:
    """Encrypt and store one entry, replacing it and keeping every other entry."""
    _checked(name)
    if not value:
        raise ValueError("an empty secret cannot be stored")
    directory = Path(config_dir)
    with _writer_lock(directory):
        encrypted = Fernet(_usable_key(directory)).encrypt(value.encode("utf-8")).decode("ascii")
        payload = _payload_for_update(directory)
        payload[name] = encrypted
        _write_payload(directory, payload)


def forget_secret(config_dir: str | Path, name: str) -> None:
    """Delete one stored entry. Nothing stored is not an error."""
    _checked(name)
    directory = Path(config_dir)
    with _writer_lock(directory):
        if not (directory / STORE_FILE_NAME).exists() and not (directory / KEY_FILE_NAME).exists():
            return
        payload = _payload_for_update(directory)
        payload.pop(name, None)
        _write_payload(directory, payload)


def store_plex_token(config_dir: str | Path, token: str) -> None:
    """Encrypt and store the Plex token, replacing any stored one."""
    if not token:
        raise ValueError("an empty token cannot be stored")
    store_secret(config_dir, PLEX_TOKEN, token)


def forget_plex_token(config_dir: str | Path) -> None:
    """Delete the stored Plex token. Nothing stored is not an error."""
    forget_secret(config_dir, PLEX_TOKEN)


def _signature(path: Path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return (None,)
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _unreadable(directory: Path, name: str, reason: str) -> TokenState:
    # Reported once per entry and distinct on-disk state, not on every request.
    signature = (str(directory), name, reason, _signature(directory / STORE_FILE_NAME),
                 _signature(directory / KEY_FILE_NAME))
    with _reported_lock:
        first = signature not in _reported
        _reported.add(signature)
    if first:
        logger.warning("secret_store_unreadable", extra={"reason": reason, "item": name})
    return TokenState("", UNREADABLE)


def read_secrets(config_dir: str | Path, names=SECRET_NAMES) -> dict[str, TokenState]:
    """Read the named entries with one pass over the files.

    Never raises and never creates a file.
    """
    directory = Path(config_dir)
    wanted = [_checked(name) for name in sorted(names)]

    def every(reason: str, only=None) -> dict[str, TokenState]:
        return {name: (_unreadable(directory, name, reason) if only is None or name in only
                       else TokenState("", ABSENT)) for name in wanted}

    try:
        raw_store = (directory / STORE_FILE_NAME).read_bytes()
    except FileNotFoundError:
        raw_store = None
    except OSError:
        return every("store_unreadable")
    try:
        key = (directory / KEY_FILE_NAME).read_bytes().strip()
    except FileNotFoundError:
        key = None
    except OSError:
        return every("key_unreadable")

    if raw_store is None:
        # A key without a store means a stored Plex token was lost.
        return every("store_missing", only={PLEX_TOKEN}) if key is not None else every("", only=set())
    try:
        payload = json.loads(raw_store.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return every("store_corrupt")
    if not isinstance(payload, dict):
        return every("store_corrupt")

    fernet = None
    key_failure = None
    if key is None:
        key_failure = "key_missing"
    else:
        try:
            fernet = Fernet(key)
        except (ValueError, TypeError):
            key_failure = "key_invalid"

    states: dict[str, TokenState] = {}
    for name in wanted:
        encrypted = payload.get(name)
        if encrypted is None:
            states[name] = TokenState("", ABSENT)
        elif not isinstance(encrypted, str):
            states[name] = _unreadable(directory, name, "store_corrupt")
        elif fernet is None:
            states[name] = _unreadable(directory, name, key_failure)
        else:
            try:
                value = fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeError, ValueError, TypeError):
                states[name] = _unreadable(directory, name, "decrypt_failed")
                continue
            states[name] = TokenState(value, STORED) if value else _unreadable(directory, name, "store_corrupt")
    return states


def read_secret(config_dir: str | Path, name: str) -> TokenState:
    """Read one stored entry. Never raises and never creates a file."""
    return read_secrets(config_dir, {name})[name]


def read_plex_token(config_dir: str | Path) -> TokenState:
    """Read the stored Plex token. Never raises and never creates a file."""
    return read_secret(config_dir, PLEX_TOKEN)
