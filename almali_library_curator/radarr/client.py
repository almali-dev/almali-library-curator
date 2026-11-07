"""Radarr v3 client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import requests

from almali_library_curator.radarr.models import (
    RadarrImportExclusion,
    RadarrMovie,
    RadarrSystemStatus,
)

logger = logging.getLogger(__name__)

API_ROOT = "/api/v3"


class RadarrError(RuntimeError):
    """Any failure while talking to Radarr."""


class RadarrNotFound(RadarrError):
    """The requested Radarr resource no longer exists."""


class RadarrAuthError(RadarrError):
    """Radarr rejected the API key."""


class RadarrUnavailableError(RadarrError):
    """Radarr could not be reached, or kept failing."""


# The generic names of the exception hierarchy, kept per package so both
# *arr clients can be imported side by side without ambiguity.
ArrError = RadarrError
ArrAuthError = RadarrAuthError
ArrUnavailableError = RadarrUnavailableError


class RadarrClient:
    """Talks to a Radarr v3 server over its HTTP API.

    The API key travels only in the X-Api-Key header, redirects are never
    followed, and every outbound request is rate limited. The mutating
    methods are single API calls; the removal lifecycle orchestrates them.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        rate_limit_per_second: float = 5.0,
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
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._next_allowed_at = 0.0

    def __repr__(self) -> str:
        return f"RadarrClient(base_url={self.base_url!r}, api_key=***)"

    @classmethod
    def from_settings(cls, settings, **overrides) -> "RadarrClient":
        """Build a client from the application settings."""
        options = {
            "base_url": settings.radarr_base_url,
            "api_key": settings.radarr_api_key,
        }
        options.update(overrides)
        return cls(**options)

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Api-Key": self._api_key,
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

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        query = dict(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "radarr_unreachable",
                    extra={"method": method, "path": path, "attempts": attempt},
                )
                raise RadarrUnavailableError(
                    f"Radarr could not be reached for {method} {path}: {error}"
                ) from None

            status = response.status_code
            if 300 <= status < 400:
                # Following a redirect would send the API key header to
                # whatever host the redirect names, so it is never followed.
                logger.warning(
                    "radarr_redirect_refused",
                    extra={"method": method, "path": path, "status": status},
                )
                raise RadarrError(
                    f"Radarr answered {status} with a redirect for {method} {path}; "
                    "refusing to follow it"
                )
            if status == 404:
                raise RadarrNotFound(f"Radarr resource not found for {method} {path}")
            if status in (401, 403):
                logger.warning(
                    "radarr_credentials_rejected",
                    extra={"method": method, "path": path},
                )
                raise RadarrAuthError(f"Radarr rejected the API key for {method} {path}")
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "radarr_error_status",
                    extra={"method": method, "path": path, "status": status},
                )
                raise RadarrUnavailableError(f"Radarr returned {status} for {method} {path}")
            if status >= 400:
                logger.warning(
                    "radarr_error_status",
                    extra={"method": method, "path": path, "status": status},
                )
                raise RadarrError(f"Radarr returned {status} for {method} {path}")

            if not response.content:
                # A delete can answer 2xx with an empty body.
                return {}
            try:
                return response.json()
            except ValueError:
                raise RadarrError(
                    f"Radarr returned an unreadable body for {method} {path}"
                ) from None

    # -- endpoints -------------------------------------------------------

    def system_status(self) -> RadarrSystemStatus:
        """The version and availability of the server."""
        payload = self._request("GET", f"{API_ROOT}/system/status")
        return RadarrSystemStatus.model_validate(payload)

    def all_movies(self) -> list[RadarrMovie]:
        """Every movie in the Radarr inventory, for external-id linking."""
        payload = self._request("GET", f"{API_ROOT}/movie")
        return [RadarrMovie.model_validate(entry) for entry in payload]

    def movie(self, radarr_id: int) -> RadarrMovie:
        """One movie by its Radarr id."""
        payload = self._request("GET", f"{API_ROOT}/movie/{radarr_id}")
        return RadarrMovie.model_validate(payload)

    def delete_movie(self, radarr_id: int, delete_files: bool = True) -> None:
        """Delete a movie and, by default, its files.

        The import exclusion is added separately by the removal lifecycle,
        so the delete itself never excludes anything.
        """
        self._request(
            "DELETE",
            f"{API_ROOT}/movie/{radarr_id}",
            params={
                "deleteFiles": "true" if delete_files else "false",
                "addImportExclusion": "false",
            },
        )

    def unmonitor_movie(self, radarr_id: int) -> RadarrMovie:
        """Stop monitoring a movie, keeping the rest of its resource intact."""
        resource = self._request("GET", f"{API_ROOT}/movie/{radarr_id}")
        resource["monitored"] = False
        payload = self._request(
            "PUT", f"{API_ROOT}/movie/{radarr_id}", json_body=resource
        )
        return RadarrMovie.model_validate(payload)

    def add_import_exclusion(self, tmdb_id: int, title: str) -> RadarrImportExclusion:
        """Exclude the movie from future imports so it is not downloaded again."""
        payload = self._request(
            "POST",
            f"{API_ROOT}/importlistexclusion",
            json_body={"tmdbId": tmdb_id, "title": title},
        )
        return RadarrImportExclusion.model_validate(payload)
