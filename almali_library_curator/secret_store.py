"""Encrypted, owner-only storage of the Plex token (S12).

The token is encrypted with Fernet (``cryptography``) and written to
``<config_dir>/secrets.json``; the key lives in ``<config_dir>/secrets.key``.
Both files are mode 0600. This protects a leaked settings file, backup or
screenshot, not a fully compromised config directory (L70).

Concurrency: the key is published with ``os.link`` from a fully written
private temporary file, so concurrent Gunicorn workers agree on one key and
nobody reads a half-written key. Writers serialise on an owner-only lock file
and replace ``secrets.json`` atomically with ``os.replace``.

A missing, corrupted or undecryptable store, or a missing or wrong key, never
raises on read: the token reads as empty with status ``unreadable`` and a
structured log event that carries the failure reason and never a secret.
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

ABSENT = "absent"
STORED = "stored"
UNREADABLE = "unreadable"

MASK = "••••"

_reported: set[tuple] = set()
_reported_lock = threading.Lock()


@dataclass(frozen=True)
class TokenState:
    """The stored Plex token and how it was read. The token never reaches repr()."""

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


@contextmanager
def _writer_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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

def store_plex_token(config_dir: str | Path, token: str) -> None:
    """Encrypt and store the Plex token, replacing any stored one."""
    if not token:
        raise ValueError("an empty token cannot be stored")
    directory = Path(config_dir)
    with _writer_lock(directory):
        encrypted = Fernet(_usable_key(directory)).encrypt(token.encode("utf-8")).decode("ascii")
        payload = _payload_for_update(directory)
        payload[PLEX_TOKEN] = encrypted
        _write_payload(directory, payload)


def forget_plex_token(config_dir: str | Path) -> None:
    """Delete the stored Plex token. Nothing stored is not an error."""
    directory = Path(config_dir)
    with _writer_lock(directory):
        if not (directory / STORE_FILE_NAME).exists() and not (directory / KEY_FILE_NAME).exists():
            return
        payload = _payload_for_update(directory)
        payload.pop(PLEX_TOKEN, None)
        _write_payload(directory, payload)


def _signature(path: Path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return (None,)
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _unreadable(directory: Path, reason: str) -> TokenState:
    # Reported once per distinct on-disk state, not on every request.
    signature = (str(directory), reason, _signature(directory / STORE_FILE_NAME),
                 _signature(directory / KEY_FILE_NAME))
    with _reported_lock:
        first = signature not in _reported
        _reported.add(signature)
    if first:
        logger.warning("secret_store_unreadable", extra={"reason": reason, "item": PLEX_TOKEN})
    return TokenState("", UNREADABLE)


def read_plex_token(config_dir: str | Path) -> TokenState:
    """Read the stored Plex token. Never raises and never creates a file."""
    directory = Path(config_dir)
    try:
        raw_store = (directory / STORE_FILE_NAME).read_bytes()
    except FileNotFoundError:
        raw_store = None
    except OSError:
        return _unreadable(directory, "store_unreadable")
    try:
        key = (directory / KEY_FILE_NAME).read_bytes().strip()
    except FileNotFoundError:
        key = None
    except OSError:
        return _unreadable(directory, "key_unreadable")

    if raw_store is None:
        # A key without a store means a stored token was lost.
        return _unreadable(directory, "store_missing") if key is not None else TokenState("", ABSENT)
    try:
        payload = json.loads(raw_store.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _unreadable(directory, "store_corrupt")
    if not isinstance(payload, dict):
        return _unreadable(directory, "store_corrupt")
    encrypted = payload.get(PLEX_TOKEN)
    if encrypted is None:
        return TokenState("", ABSENT)
    if not isinstance(encrypted, str):
        return _unreadable(directory, "store_corrupt")
    if key is None:
        return _unreadable(directory, "key_missing")
    try:
        fernet = Fernet(key)
    except (ValueError, TypeError):
        return _unreadable(directory, "key_invalid")
    try:
        token = fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        return _unreadable(directory, "decrypt_failed")
    if not token:
        return _unreadable(directory, "store_corrupt")
    return TokenState(token, STORED)
