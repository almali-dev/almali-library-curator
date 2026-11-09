"""Tautulli v2 API client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from almali_library_curator.tautulli.models import TautulliHistoryEntry, TautulliUser

logger = logging.getLogger(__name__)


class TautulliError(RuntimeError):
    """Any failure while talking to Tautulli."""


class TautulliAuthError(TautulliError):
    """Tautulli rejected the API key."""


class TautulliUnavailableError(TautulliError):
    """Tautulli could not be reached, or kept failing."""


class TautulliClient:
    """Reads Tautulli's v2 HTTP API.

    The v2 API only authenticates through its ``apikey`` parameter, so every
    command travels as a POST whose body carries the key: it never lands in
    a URL, where a log record or an error message could pick it up. Every
    outbound request is rate limited and redirects are never followed.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        rate_limit_per_second: float = 5.0,
        page_size: int = 1000,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.rate_limit_per_second = rate_limit_per_second
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._next_allowed_at = 0.0

    def __repr__(self) -> str:
        return f"TautulliClient(base_url={self.base_url!r}, api_key=***)"

    @classmethod
    def from_settings(cls, settings, **overrides) -> "TautulliClient":
        """Build a client from the application settings."""
        options = {
            "base_url": settings.tautulli_base_url,
            "api_key": settings.tautulli_api_key,
        }
        options.update(overrides)
        return cls(**options)

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def _throttle(self) -> None:
        if self.rate_limit_per_second <= 0:
            return
        now = self._clock()
        wait = self._next_allowed_at - now
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        self._next_allowed_at = now + (1.0 / self.rate_limit_per_second)

    def _backoff(self, attempt: int) -> None:
        if self.backoff_seconds > 0:
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _post(self, cmd: str, params: dict[str, str] | None = None) -> object:
        """Run one API command; the API key rides in the POST body."""
        body = {"cmd": cmd, "apikey": self._api_key}
        body.update(
            {
                key: str(value)
                for key, value in (params or {}).items()
                if value is not None
            }
        )
        url = f"{self.base_url}/api/v2"

        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.post(
                    url,
                    data=body,
                    headers=self._headers(),
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "tautulli_unreachable", extra={"cmd": cmd, "attempts": attempt}
                )
                raise TautulliUnavailableError(
                    f"Tautulli could not be reached for {cmd}: {error}"
                ) from None

            status = response.status_code
            if 300 <= status < 400:
                # Following a redirect would leak the apikey body parameter
                # to whatever host the redirect names, so it never happens.
                logger.warning(
                    "tautulli_redirect_refused", extra={"cmd": cmd, "status": status}
                )
                raise TautulliError(
                    f"Tautulli answered {status} with a redirect for {cmd}; "
                    "refusing to follow it"
                )
            if status in (401, 403):
                logger.warning("tautulli_credentials_rejected", extra={"cmd": cmd})
                raise TautulliAuthError(f"Tautulli rejected the API key for {cmd}")
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "tautulli_error_status", extra={"cmd": cmd, "status": status}
                )
                raise TautulliUnavailableError(f"Tautulli returned {status} for {cmd}")
            if status >= 400:
                logger.warning(
                    "tautulli_error_status", extra={"cmd": cmd, "status": status}
                )
                raise TautulliError(f"Tautulli returned {status} for {cmd}")

            try:
                payload = response.json()
            except ValueError:
                raise TautulliError(
                    f"Tautulli returned an unreadable body for {cmd}"
                ) from None
            if not isinstance(payload, dict):
                raise TautulliError(f"Tautulli returned an unexpected body for {cmd}")
            envelope = payload.get("response")
            if not isinstance(envelope, dict):
                raise TautulliError(f"Tautulli returned an unexpected body for {cmd}")
            if envelope.get("result") != "success":
                detail = str(envelope.get("message", "unknown error"))
                if self._api_key:
                    detail = detail.replace(self._api_key, "***")
                # Service-supplied diagnostics can echo submitted credentials.
                logger.warning("tautulli_command_error", extra={"cmd": cmd})
                raise TautulliError(f"Tautulli failed {cmd}: {detail}")
            return envelope.get("data")

    # -- endpoints -------------------------------------------------------

    def history(
        self,
        user_id: int | str | None = None,
        after: str | None = None,
    ) -> list[TautulliHistoryEntry]:
        """The play history, optionally for one user and from a date.

        Tautulli pages get_history with start/length; every page is read.
        ``after`` is Tautulli's own date filter (YYYY-MM-DD).
        """
        params: dict[str, str] = {"length": str(self.page_size)}
        if user_id is not None:
            params["user_id"] = str(user_id)
        if after is not None:
            params["after"] = str(after)

        entries: list[TautulliHistoryEntry] = []
        start = 0
        while True:
            page_params = dict(params)
            page_params["start"] = str(start)
            data = self._post("get_history", page_params)
            if not isinstance(data, dict):
                raise TautulliError("Tautulli returned an unexpected history body")
            rows = data.get("data") or []
            if not isinstance(rows, list):
                raise TautulliError("Tautulli returned an unexpected history body")
            entries.extend(TautulliHistoryEntry.model_validate(row) for row in rows)
            total = data.get("recordsFiltered", data.get("recordsTotal"))
            start += len(rows)
            if not rows or (total is not None and start >= int(total)):
                break
        return entries

    def users(self) -> list[TautulliUser]:
        """The user accounts Tautulli knows about."""
        data = self._post("get_users")
        if not isinstance(data, list):
            raise TautulliError("Tautulli returned an unexpected users body")
        return [TautulliUser.model_validate(entry) for entry in data]
