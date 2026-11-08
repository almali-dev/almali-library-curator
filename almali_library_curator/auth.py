"""The one owner login, its session and the brute-force guard (S17, S21).

The credentials come from ``ALC_AUTH_USER`` and ``ALC_AUTH_PASSWORD`` only.
The password is hashed once at start with Werkzeug and the plain text is
never kept, stored or logged. Without both variables the app fails closed:
every screen and action answers a translated 503 page (rule 2).

S21 adds three things to the session: an absolute lifetime measured from the
sign-in, whatever the use (rule 1), a revocation epoch every session proof is
folded with, so a sign-out ends every session in every browser (rule 2), and
a brute-force guard whose state is bounded (rule 3).
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
from itertools import islice
import logging
import os
from pathlib import Path
import re
from secrets import token_hex
import stat
import tempfile
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit

from werkzeug.security import check_password_hash, generate_password_hash

from almali_library_curator.config import ENV_PREFIX

logger = logging.getLogger(__name__)

USER_VARIABLE = f"{ENV_PREFIX}AUTH_USER"
PASSWORD_VARIABLE = f"{ENV_PREFIX}AUTH_PASSWORD"
SECURE_COOKIES_VARIABLE = f"{ENV_PREFIX}SECURE_COOKIES"
TRUSTED_PROXIES_VARIABLE = f"{ENV_PREFIX}TRUSTED_PROXIES"

SESSION_HOURS = 12
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60

# S21 rule 3: the guard keeps at most this many client addresses in memory.
MAX_TRACKED_ADDRESSES = 1024
# A full sweep of the tracked addresses runs every CLEANUP_INTERVAL attempts,
# so the cost of the cleanup stays constant on average.
CLEANUP_INTERVAL = 64
# How many addresses an eviction looks at before it gives up.
EVICTION_SCAN = 32

# Reachable without a session (rule 3); ``healthz`` and ``static`` stay
# reachable even while the login is not configured (rule 2).
PUBLIC_ENDPOINTS = frozenset({"healthz", "static", "login"})
ALWAYS_PUBLIC_ENDPOINTS = frozenset({"healthz", "static"})

SESSION_USER = "auth_user"
SESSION_PROOF = "auth_proof"
# S21 rule 1: the wall-clock moment of the sign-in, in the session itself.
SESSION_SIGNED_IN_AT = "auth_signed_in_at"

# S21 rule 2: the revocation epoch, in the config directory.
SESSION_EPOCH_FILE = "session_epoch"
SESSION_EPOCH_LOCK_FILE = ".session_epoch.lock"
_EPOCH_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Credentials:
    """The owner's user name and the password hash; never the plain text."""

    user: str = field(repr=False)
    password_hash: str = field(repr=False)
    # HMAC of the user and the plain password under the shared signing key,
    # made once at start: the same on every worker and after a restart, and
    # different as soon as the password changes. The plain text is not kept.
    session_proof: str = field(repr=False, default="")
    # Decided at start from the plain text: a user name that contains the
    # password is never logged (S17 rule 10c).
    user_loggable: bool = field(repr=False, default=False)

    def matches(self, user: str, password: str) -> bool:
        # Both checks always run, so the answer time never says which failed.
        user_ok = hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8"))
        password_ok = check_password_hash(self.password_hash, password)
        return user_ok and password_ok

    def safe_log_user(self) -> str:
        return self.user if self.user_loggable else "***"

    def proof(self, epoch: str = "") -> str:
        """The session marker bound to this password and the current epoch."""
        return fold_epoch(self.session_proof, epoch)

    def signed_in(self, user: object, proof: object, epoch: str = "") -> bool:
        """True when a session carries this owner and this password's proof."""
        if not isinstance(user, str) or not isinstance(proof, str) or not self.session_proof:
            return False
        expected = self.proof(epoch)
        return hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8")) and (
            hmac.compare_digest(proof.encode("utf-8"), expected.encode("utf-8"))
        )


def fold_epoch(base_proof: str, epoch: str) -> str:
    """Bind a session proof to the revocation epoch (S21 rule 2).

    The epoch is folded into the proof, not stored beside it: a session
    issued before a sign-out carries a proof the current epoch does not
    produce, so it is signed out without the cookie saying anything about
    the epoch.
    """
    if not base_proof:
        return ""
    material = base_proof.encode("utf-8") + b"\0" + epoch.encode("utf-8")
    return hmac.new(material, b"alc-session-epoch", hashlib.sha256).hexdigest()


def session_expired(signed_in_at: object, now: float, hours: int = SESSION_HOURS) -> bool:
    """True when the session is older than the absolute lifetime (S21 rule 1).

    A session without a sign-in time - one issued before this change - is
    expired too, and no use of a session extends it.
    """
    if isinstance(signed_in_at, bool) or not isinstance(signed_in_at, (int, float)):
        return True
    return now - float(signed_in_at) >= hours * 3600


class SessionEpoch:
    """The revocation counter every session proof is folded with (S21 rule 2).

    The epoch lives in an owner-only file in the config directory, so every
    worker and every restart share it. A sign-out advances the epoch, which
    signs every session issued so far out, in every browser. A missing or
    unreadable file never stops the app: it reads as a fresh epoch, and the
    next sign-in or sign-out rewrites it.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock_path = self._path.with_name(SESSION_EPOCH_LOCK_FILE)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> str:
        """The epoch in force, or '' when there is none to read."""
        try:
            raw = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        token = raw.strip()
        if not _EPOCH_PATTERN.fullmatch(token):
            return ""
        try:
            if stat.S_IMODE(self._path.stat().st_mode) != 0o600:
                os.chmod(self._path, 0o600)
        except OSError:
            pass
        return token

    def ensure(self) -> str:
        """The epoch in force, writing a fresh one when there is none."""
        with self._lock:
            return self._write(token_hex(16), only_if_missing=True)[0]

    def advance(self) -> bool:
        """Advance the epoch; False when the previous one stayed in force."""
        with self._lock:
            return self._write(token_hex(16))[1]

    def _write(self, token: str, *, only_if_missing: bool = False) -> tuple[str, bool]:
        """Replace the file atomically and owner-only; it never raises.

        The file is written whole to a private temporary file and then
        renamed into place, so a reader never sees a partial epoch. The
        cross-process lock also serialises the first creation: when
        ``only_if_missing`` is set, the epoch is re-read under that lock and
        another worker's value wins. When the write fails the previous epoch
        stays in force and the app keeps running.
        """
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            logger.warning("session_epoch_write_failed", extra={"path": str(self._path)})
            return self.current(), False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if only_if_missing:
                existing = self.current()
                if existing:
                    return existing, True
            handle, temporary = tempfile.mkstemp(dir=directory, prefix=".session_epoch.tmp.")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(token)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
            except BaseException:
                os.unlink(temporary)
                raise
            return token, True
        except OSError:
            logger.warning("session_epoch_write_failed", extra={"path": str(self._path)})
            return self.current(), False
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass


def credentials_from_environment(env: Mapping[str, str], signing_key: bytes) -> Credentials | None:
    """The login, or ``None`` when either variable is unset or blank."""
    user = (env.get(USER_VARIABLE) or "").strip()
    password = env.get(PASSWORD_VARIABLE) or ""
    if not user or not password.strip():
        return None
    material = b"alc-session\0" + user.encode("utf-8") + b"\0" + password.encode("utf-8")
    return Credentials(
        user=user,
        password_hash=generate_password_hash(password),
        session_proof=hmac.new(signing_key, material, hashlib.sha256).hexdigest(),
        user_loggable=password not in user,
    )


def secure_cookies_from_environment(env: Mapping[str, str]) -> bool:
    return (env.get(SECURE_COOKIES_VARIABLE) or "").strip().lower() in {"1", "true", "yes", "on"}


def trusted_proxies_from_environment(env: Mapping[str, str]) -> int:
    raw = (env.get(TRUSTED_PROXIES_VARIABLE) or "").strip()
    try:
        count = int(raw) if raw else 0
    except ValueError:
        count = -1
    if count < 0:
        logger.warning("invalid_trusted_proxies", extra={"reason": "invalid"})
    return max(count, 0)


def safe_next(target: str | None) -> str:
    """A relative path on this app, or Library health (no open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


@dataclass
class _AddressState:
    """What the guard remembers about one client address."""

    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    lock_version: int = 0
    pending: set[int] = field(default_factory=set)


class LoginGuard:
    """In-process brute-force guard per client address (rule 6, S21 rule 3).

    At most ``max_addresses`` client addresses are kept in memory. The
    expired ones are cleaned up first, and an address with an active lock is
    never evicted.
    """

    def __init__(self, clock: Callable[[], float] | None = None, *,
                 max_addresses: int = MAX_TRACKED_ADDRESSES):
        self._clock = clock or time.monotonic
        self._max_addresses = max_addresses
        self._tracked: OrderedDict[str, _AddressState] = OrderedDict()
        self._next_token = 0
        self._attempts = 0
        self._lock = threading.RLock()

    def tracked_addresses(self) -> int:
        """How many client addresses the guard keeps in memory (S21 rule 3)."""
        with self._lock:
            return len(self._tracked)

    def locked(self, address: str) -> bool:
        with self._lock:
            state = self._tracked.get(address)
            if state is None:
                return False
            return self._is_locked(state)

    def _is_locked(self, state: _AddressState) -> bool:
        now = self._clock()
        if state.locked_until > now:
            return True
        if state.locked_until:
            # The lock has run out: the address is free again and its
            # failures are dropped, exactly as they were before.
            state.locked_until = 0.0
            state.failures.clear()
        return False

    @staticmethod
    def _prune_failures(state: _AddressState, now: float) -> None:
        while state.failures and now - state.failures[0] >= FAILURE_WINDOW_SECONDS:
            state.failures.popleft()

    def admit(self, address: str) -> tuple[int, int] | None:
        """Atomically admit one password check, unless the address is locked."""
        with self._lock:
            self._attempts += 1
            if self._attempts >= CLEANUP_INTERVAL:
                self._attempts = 0
                self._sweep()
            state = self._tracked.get(address)
            if state is not None:
                self._tracked.move_to_end(address)
                if self._is_locked(state):
                    return None
            now = self._clock()
            if state is None:
                self._make_room()
                if len(self._tracked) < self._max_addresses:
                    state = _AddressState()
                    self._tracked[address] = state
                else:
                    # Every slot holds an active lock or pending attempt. A new
                    # address is refused instead of bypassing the guard.
                    logger.warning("login_guard_state_full", extra={"address": address})
                    return None
            self._prune_failures(state, now)
            pending = state.pending
            # A pending attempt counts as a failure until it is answered.
            if len(state.failures) + len(pending) >= MAX_FAILURES:
                return None
            token = self._next_token
            self._next_token += 1
            pending.add(token)
            return token, state.lock_version

    def failed(self, address: str, attempt: tuple[int, int]) -> None:
        token, _ = attempt
        with self._lock:
            state = self._tracked.get(address)
            if state is None or token not in state.pending:
                return
            state.pending.discard(token)
            now = self._clock()
            state.failures.append(now)
            self._prune_failures(state, now)
            # Pending attempts count, as at admission (S17 rule 10a).
            if len(state.failures) + len(state.pending) >= MAX_FAILURES and state.locked_until <= now:
                state.locked_until = now + LOCK_SECONDS
                state.lock_version += 1

    def succeeded(self, address: str, attempt: tuple[int, int]) -> None:
        token, admitted_version = attempt
        with self._lock:
            state = self._tracked.get(address)
            if state is None:
                return
            state.pending.discard(token)
            if admitted_version == state.lock_version and not state.locked_until:
                state.failures.clear()

    def _make_room(self) -> None:
        """Room for one more address, without ever evicting an active lock."""
        if len(self._tracked) < self._max_addresses:
            return
        if self._evict_one():
            return
        # Nothing may be evicted: the expired state goes first.
        self._sweep()
        self._evict_one()

    def _evict_one(self) -> bool:
        """Evict one address that may be forgotten; False when none may be."""
        now = self._clock()
        fallback = None
        skipped_locked = []
        evictable_seen = 0
        victim = None
        for address, state in self._tracked.items():
            if state.locked_until > now or state.pending:
                skipped_locked.append(address)
                continue  # an active lock is never evicted
            if not state.failures or now - state.failures[-1] >= FAILURE_WINDOW_SECONDS:
                victim = address
                break
            if fallback is None:
                fallback = address
            evictable_seen += 1
            if evictable_seen >= EVICTION_SCAN:
                break
        for address in skipped_locked:
            self._tracked.move_to_end(address)
        victim = victim or fallback
        if victim is not None:
            del self._tracked[victim]
            return True
        return False

    def _sweep(self) -> None:
        """Drop the expired state; the addresses that stay keep their history."""
        now = self._clock()
        for address, state in list(self._tracked.items()):
            self._prune_failures(state, now)
            if state.locked_until and state.locked_until <= now:
                state.locked_until = 0.0
            if not state.failures and not state.pending and not state.locked_until:
                del self._tracked[address]
