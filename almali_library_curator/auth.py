"""The one owner login, its session and the brute-force guard (S17).

The credentials come from ``ALC_AUTH_USER`` and ``ALC_AUTH_PASSWORD`` only.
The password is hashed once at start with Werkzeug and the plain text is
never kept, stored or logged. Without both variables the app fails closed:
every screen and action answers a translated 503 page (rule 2).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import logging
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

# Reachable without a session (rule 3); ``healthz`` and ``static`` stay
# reachable even while the login is not configured (rule 2).
PUBLIC_ENDPOINTS = frozenset({"healthz", "static", "login"})
ALWAYS_PUBLIC_ENDPOINTS = frozenset({"healthz", "static"})

SESSION_USER = "auth_user"
SESSION_PROOF = "auth_proof"


@dataclass(frozen=True)
class Credentials:
    """The owner's user name and the password hash; never the plain text."""

    user: str = field(repr=False)
    password_hash: str = field(repr=False)
    # HMAC of the user and the plain password under the shared signing key,
    # made once at start: the same on every worker and after a restart, and
    # different as soon as the password changes. The plain text is not kept.
    session_proof: str = field(repr=False, default="")

    def matches(self, user: str, password: str) -> bool:
        # Both checks always run, so the answer time never says which failed.
        user_ok = hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8"))
        password_ok = check_password_hash(self.password_hash, password)
        return user_ok and password_ok

    def safe_log_user(self) -> str:
        return "***" if check_password_hash(self.password_hash, self.user) else self.user

    def proof(self) -> str:
        """The session marker bound to this password: a new password ends old sessions."""
        return self.session_proof

    def signed_in(self, user: object, proof: object) -> bool:
        """True when a session carries this owner and this password's proof."""
        if not isinstance(user, str) or not isinstance(proof, str) or not self.session_proof:
            return False
        return hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8")) and (
            hmac.compare_digest(proof.encode("utf-8"), self.session_proof.encode("utf-8"))
        )


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


class LoginGuard:
    """In-process brute-force guard per client address (rule 6)."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._pending: dict[str, set[int]] = {}  # admitted, not yet answered
        self._lock_versions: dict[str, int] = {}
        self._next_token = 0
        self._lock = threading.RLock()

    def locked(self, address: str) -> bool:
        with self._lock:
            until = self._locked_until.get(address)
            if until is None:
                return False
            if self._clock() < until:
                return True
            del self._locked_until[address]
            self._failures.pop(address, None)
            return False

    def _answer(self, address: str, token: int) -> bool:
        """Forget one admitted attempt; False when it was already answered."""
        pending = self._pending.get(address)
        if pending is None or token not in pending:
            return False
        pending.discard(token)
        if not pending:
            del self._pending[address]
        return True

    def admit(self, address: str) -> tuple[int, int] | None:
        """Atomically admit one password check, unless the address is locked."""
        with self._lock:
            if self.locked(address):
                return None
            now = self._clock()
            failures = self._failures.setdefault(address, deque())
            while failures and now - failures[0] >= FAILURE_WINDOW_SECONDS:
                failures.popleft()
            pending = self._pending.setdefault(address, set())
            # A pending attempt counts as a failure until it is answered.
            if len(failures) + len(pending) >= MAX_FAILURES:
                return None
            token = self._next_token
            self._next_token += 1
            pending.add(token)
            return token, self._lock_versions.get(address, 0)

    def failed(self, address: str, attempt: tuple[int, int]) -> None:
        token, _ = attempt
        with self._lock:
            if not self._answer(address, token):
                return
            now = self._clock()
            failures = self._failures.setdefault(address, deque())
            failures.append(now)
            while failures and now - failures[0] >= FAILURE_WINDOW_SECONDS:
                failures.popleft()
            # Pending attempts count, as at admission (S17 rule 10a).
            if len(failures) + len(self._pending.get(address, ())) >= MAX_FAILURES and self._locked_until.get(address, 0) <= now:
                self._locked_until[address] = now + LOCK_SECONDS
                self._lock_versions[address] = self._lock_versions.get(address, 0) + 1
            if len(self._failures) > 1024:
                for stale in [key for key, times in self._failures.items()
                              if not times or now - times[-1] >= FAILURE_WINDOW_SECONDS]:
                    del self._failures[stale]

    def succeeded(self, address: str, attempt: tuple[int, int]) -> None:
        token, admitted_version = attempt
        with self._lock:
            self._answer(address, token)
            if admitted_version == self._lock_versions.get(address, 0) and not self._locked_until.get(address):
                self._failures.pop(address, None)
