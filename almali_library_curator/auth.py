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
PASSWORD_FILE_VARIABLE = f"{ENV_PREFIX}AUTH_PASSWORD_FILE"
PASSWORD_HASH_VARIABLE = f"{ENV_PREFIX}AUTH_PASSWORD_HASH"
SECURE_COOKIES_VARIABLE = f"{ENV_PREFIX}SECURE_COOKIES"
TRUSTED_PROXIES_VARIABLE = f"{ENV_PREFIX}TRUSTED_PROXIES"
# #71: a plain password shorter than this is reported at start (never
# refused, so an existing installation keeps working) and the same minimum
# the Unraid installer asks for.
MIN_PASSWORD_LENGTH = 8
# The header that shows a reverse proxy in front of the app (#71).
FORWARDED_HEADER = "X-Forwarded-For"

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


# S3 (#115): the recovery an owner needs when the hash cannot be read. It
# names the variables only, never a value, so it can go to the log and the
# page as it is.
HASH_RECOVERY_MESSAGE = (
    f"fix {PASSWORD_HASH_VARIABLE} so it holds a complete Werkzeug password hash, "
    f"or remove it so {PASSWORD_VARIABLE} or {PASSWORD_FILE_VARIABLE} is used, "
    "then restart the container"
)
# S3 (#115): the one problem that needs its own page. Every other reason the
# login is unusable keeps the "login is not configured" page.
AUTH_HASH_INVALID = "hash_invalid"


@dataclass(frozen=True)
class LoginSetup:
    """The login read at start, and why it is unusable when it is.

    ``problem`` is empty when ``credentials`` is set; ``AUTH_HASH_INVALID``
    means ``ALC_AUTH_PASSWORD_HASH`` is set but is not a usable Werkzeug
    hash, so the plain password and the password file are not used and the
    app stays closed (S3, #115).
    """

    credentials: Credentials | None = None
    problem: str = ""


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


def _password_from_file(path: str) -> str | None:
    """The password in ``ALC_AUTH_PASSWORD_FILE``, or ``None`` when it cannot be read.

    A trailing line break is not part of the password: a file written by
    ``printf '%s\\n'`` and one written by ``printf '%s'`` carry the same
    secret. A file any other user can read is reported, never refused, so an
    existing installation keeps working while the owner fixes the mode.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("auth_password_file_unreadable", extra={"path": path})
        return None
    try:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
    except OSError:
        mode = None
    if mode is not None and mode & 0o077:
        logger.warning("auth_password_file_permissions",
                       extra={"path": path, "mode": oct(mode)})
    return text.rstrip("\r\n")


# The hash formats Werkzeug's ``generate_password_hash`` writes: scrypt and
# pbkdf2. A hash with any other shape is refused at start instead of failing
# on every login attempt.
_PASSWORD_HASH_PATTERN = re.compile(
    r"(?:scrypt:\d+:\d+:\d+|pbkdf2:[a-z0-9]+:\d+)\$[^$]+\$[0-9a-fA-F]+")


def _password_hash_is_usable(value: str) -> bool:
    """True when the stored value is a complete Werkzeug password hash."""
    if not _PASSWORD_HASH_PATTERN.fullmatch(value):
        return False
    try:
        check_password_hash(value, "")
    except (ValueError, TypeError):
        return False
    return True


def login_from_environment(env: Mapping[str, str], signing_key: bytes) -> LoginSetup:
    """The login, or the reason there is none.

    #71: the password comes from the first source that is set, in this order:
    ``ALC_AUTH_PASSWORD_HASH`` (a Werkzeug hash, never the clear text),
    ``ALC_AUTH_PASSWORD_FILE`` (an owner-only file) and the plain
    ``ALC_AUTH_PASSWORD``. More than one source is reported, so a leftover
    clear-text variable can never silently win over the safer one. Anything
    unreadable or malformed fails closed: the app answers the 503 page
    instead of opening with a guess.
    """
    user = (env.get(USER_VARIABLE) or "").strip()
    plain = env.get(PASSWORD_VARIABLE) or ""
    file_path = (env.get(PASSWORD_FILE_VARIABLE) or "").strip()
    stored_hash = (env.get(PASSWORD_HASH_VARIABLE) or "").strip()
    sources = [name for name, value in (
        (PASSWORD_VARIABLE, plain.strip()),
        (PASSWORD_FILE_VARIABLE, file_path),
        (PASSWORD_HASH_VARIABLE, stored_hash),
    ) if value]
    if stored_hash and not _password_hash_is_usable(stored_hash):
        # S3 (#115): the hash fails closed, as before, but the line now
        # names the variable and the way out, and still no value. It is
        # checked before the user, so a missing ALC_AUTH_USER does not hide
        # it behind the generic "login missing" page.
        logger.warning(
            "auth_password_hash_invalid",
            extra={
                "variable": PASSWORD_HASH_VARIABLE,
                "problem": AUTH_HASH_INVALID,
                "recovery": HASH_RECOVERY_MESSAGE,
            },
        )
        return LoginSetup(problem=AUTH_HASH_INVALID)
    if not user or not sources:
        return LoginSetup()
    if len(sources) > 1:
        logger.warning("auth_password_sources_conflict",
                       extra={"sources": ",".join(sources)})
    # The material that decides the session proof: the plain password when it
    # is known, the stored hash otherwise. Changing either signs old sessions
    # out; the plain text is never kept when a hash was given.
    material = ""
    password_hash = ""
    if stored_hash:
        material = stored_hash
        password_hash = stored_hash
    else:
        password = _password_from_file(file_path) if file_path else plain
        if password is None or not password.strip():
            return LoginSetup()
        if len(password) < MIN_PASSWORD_LENGTH:
            logger.warning("auth_password_short",
                           extra={"min_length": MIN_PASSWORD_LENGTH})
        material = password
        password_hash = generate_password_hash(password)
    proof = b"alc-session\0" + user.encode("utf-8") + b"\0" + material.encode("utf-8")
    return LoginSetup(credentials=Credentials(
        user=user,
        password_hash=password_hash,
        session_proof=hmac.new(signing_key, proof, hashlib.sha256).hexdigest(),
        user_loggable=material not in user,
    ))


def forwarded_headers_present(headers: Mapping[str, str]) -> bool:
    """True when the request carries ``X-Forwarded-For`` (#71)."""
    return bool((headers.get(FORWARDED_HEADER) or "").strip())


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
                    # Every slot holds live failure state. A new address is
                    # refused instead of bypassing the guard.
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
        """Room for one more address, without evicting live failure state."""
        if len(self._tracked) < self._max_addresses:
            return
        if self._evict_one():
            return
        # Expired entries are removed by the periodic sweep.

    def _evict_one(self) -> bool:
        """Evict one address whose failure state has expired."""
        now = self._clock()
        protected = []
        live_failures = 0
        victim = None
        for address, state in self._tracked.items():
            if state.locked_until > now or state.pending:
                protected.append(address)
                continue
            if state.failures and now - state.failures[-1] < FAILURE_WINDOW_SECONDS:
                protected.append(address)
                live_failures += 1
                if live_failures >= EVICTION_SCAN:
                    break
                continue
            victim = address
            break
        for address in protected:
            self._tracked.move_to_end(address)
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
