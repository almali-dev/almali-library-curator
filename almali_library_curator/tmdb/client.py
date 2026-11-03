"""TMDB API client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from library_curator.tmdb.models import TmdbMovie, TmdbShow

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.themoviedb.org"


class TmdbError(RuntimeError):
    """Any failure while talking to TMDB."""


class TmdbAuthError(TmdbError):
    """TMDB rejected the access token."""


class TmdbUnavailableError(TmdbError):
    """TMDB could not be reached, or kept failing."""


class TmdbClient:
    """Reads TMDB over its v3 endpoints with v4 authentication.

    The v4 access token rides only in the Authorization header, never in a
    query parameter, so it never lands in a logged or raised URL. Every
    outbound request is rate limited and redirects are never followed.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        access_token: str = "",
        timeout: float = 15.0,
        rate_limit_per_second: float = 5.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._access_token = access_token
        self.timeout = timeout
        self.rate_limit_per_second = rate_limit_per_second
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._next_allowed_at = 0.0

    def __repr__(self) -> str:
        return f"TmdbClient(base_url={self.base_url!r}, access_token=***)"

    @classmethod
    def from_settings(cls, settings, **overrides) -> "TmdbClient":
        """Build a client from the application settings."""
        options = {
            "base_url": settings.tmdb_base_url,
            "access_token": settings.tmdb_api_token,
        }
        options.update(overrides)
        return cls(**options)

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }

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

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"

        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.get(
                    url,
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
                    "tmdb_unreachable", extra={"path": path, "attempts": attempt}
                )
                raise TmdbUnavailableError(
                    f"TMDB could not be reached for {path}: {error}"
                ) from None

            status = response.status_code
            if 300 <= status < 400:
                # Following a redirect would send the bearer token to
                # whatever host the redirect names, so it never happens.
                logger.warning(
                    "tmdb_redirect_refused", extra={"path": path, "status": status}
                )
                raise TmdbError(
                    f"TMDB answered {status} with a redirect for {path}; "
                    "refusing to follow it"
                )
            if status in (401, 403):
                logger.warning("tmdb_credentials_rejected", extra={"path": path})
                raise TmdbAuthError(f"TMDB rejected the access token for {path}")
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "tmdb_error_status", extra={"path": path, "status": status}
                )
                raise TmdbUnavailableError(f"TMDB returned {status} for {path}")
            if status >= 400:
                logger.warning(
                    "tmdb_error_status", extra={"path": path, "status": status}
                )
                raise TmdbError(f"TMDB returned {status} for {path}")

            try:
                payload = response.json()
            except ValueError:
                raise TmdbError(f"TMDB returned an unreadable body for {path}") from None
            if not isinstance(payload, dict):
                raise TmdbError(f"TMDB returned an unexpected body for {path}")
            return payload

    # -- endpoints -------------------------------------------------------

    def movie(self, tmdb_id: int | str) -> TmdbMovie:
        """The details of one movie: rating (0-10), status, release date."""
        return TmdbMovie.model_validate(self._get(f"/3/movie/{tmdb_id}"))

    def tv(self, tmdb_id: int | str) -> TmdbShow:
        """The details of one series: rating, status, last air date."""
        return TmdbShow.model_validate(self._get(f"/3/tv/{tmdb_id}"))
