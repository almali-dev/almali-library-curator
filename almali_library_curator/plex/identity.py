"""The Plex client identifier of one installation (#145, S1).

Every copy of the app used to announce one fixed ``X-Plex-Client-Identifier``
to plex.tv and to Plex, so every installation looked like the same device and
a pending PIN could be polled with an identifier anyone could guess.

The identifier is now generated once per installation, with 128 bits of
randomness, and kept owner-only (mode 0600) in the config directory beside the
signing key. The sign-in and every Plex client of one installation send the
same value. It is written whole to a private temporary file and renamed into
place under a cross-process lock, so concurrent Gunicorn workers agree on one
identifier and no reader ever sees a partial one.

A missing, unreadable or corrupt file is replaced with a fresh identifier and
a warning is logged; no failure here ever stops the app. Nothing else is
touched, so an existing installation keeps its stored Plex token, its settings
and its database across the upgrade.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
import re
from secrets import token_hex
import stat
import tempfile
import threading

logger = logging.getLogger(__name__)

IDENTIFIER_FILE_NAME = "plex_client_identifier"
LOCK_FILE_NAME = ".plex_client_identifier.lock"
# 16 bytes: 128 bits, as the issue asks, as 32 hex characters on disk.
IDENTIFIER_BYTES = 16
_IDENTIFIER_PATTERN = re.compile(r"[0-9a-f]{32}")

# The identifier of an installation never changes, so it is read once per
# process and per config directory. The lock also serialises the first
# creation between threads; the file lock does that between processes.
_cache: dict[Path, str] = {}
_cache_lock = threading.Lock()

_process_identifier: str | None = None
_process_lock = threading.Lock()


def client_identifier(config_dir: str | Path) -> str:
    """The installation's Plex client identifier, created on first use."""
    directory = Path(config_dir)
    key = directory.expanduser().absolute()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        value = _read_or_create(directory)
        _cache[key] = value
        return value


def process_identifier() -> str:
    """One random identifier per process, for a client with no installation.

    A client or a sign-in built without a config directory (a script, a test)
    has no installation to read. It then uses one random identifier for the
    life of the process instead of a value shared by every installation.
    """
    global _process_identifier
    with _process_lock:
        if _process_identifier is None:
            _process_identifier = token_hex(IDENTIFIER_BYTES)
        return _process_identifier


def _read_or_create(directory: Path) -> str:
    """The stored identifier, or a fresh one written in its place."""
    path = directory / IDENTIFIER_FILE_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(directory / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        # A config directory that cannot be written never stops the app: the
        # process keeps one identifier, and the next start tries again.
        logger.warning("plex_client_identifier_unwritable", extra={"path": str(directory)})
        return process_identifier()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        stored, problem = _stored_identifier(path)
        if stored is not None:
            return stored
        fresh = token_hex(IDENTIFIER_BYTES)
        if problem is not None:
            logger.warning("plex_client_identifier_replaced",
                           extra={"reason": problem, "path": str(path)})
        try:
            _write(path, fresh)
        except OSError:
            logger.warning("plex_client_identifier_write_failed", extra={"path": str(path)})
        return fresh
    finally:
        os.close(lock_fd)


def _stored_identifier(path: Path) -> tuple[str | None, str | None]:
    """The identifier in ``path``, and why it is unusable when it is."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None  # a fresh install: nothing to report
    except OSError:
        return None, "unreadable"
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None, "corrupt"
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        return None, "corrupt"
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass
    return value, None


def _write(path: Path, value: str) -> None:
    """Replace ``path`` atomically with an owner-only file holding ``value``."""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
