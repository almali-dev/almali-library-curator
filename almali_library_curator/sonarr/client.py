"""Sonarr v3 client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from almali_library_curator.sonarr.models import (
    SonarrEpisodeFile,
    SonarrImportExclusion,
    SonarrSeries,
    SonarrSystemStatus,
)
from almali_library_curator.request_journal import DEFAULT_JOURNAL_SIZE, RequestJournal

logger = logging.getLogger(__name__)

API_ROOT = "/api/v3"


class SonarrError(RuntimeError):
    """Any failure while talking to Sonarr."""


class SonarrNotFound(SonarrError):
    """The requested Sonarr resource no longer exists."""

    # Marks the resource as gone, so an arr-agnostic caller can tell a
    # 404 from any other refusal without importing this package (#69).
    resource_gone = True


class SonarrAuthError(SonarrError):
    """Sonarr rejected the API key."""


class SonarrUnavailableError(SonarrError):
    """Sonarr could not be reached, or kept failing."""


# The generic names of the exception hierarchy, kept per package so both
# *arr clients can be imported side by side without ambiguity.
ArrError = SonarrError
ArrAuthError = SonarrAuthError
ArrUnavailableError = SonarrUnavailableError


@dataclass(frozen=True)
class MonitoringChange:
    """One monitoring write: the series it leaves behind, and whether it changed it.

    The removal lifecycle records only the side effects it applied (#103),
    and the retry of a removal re-applies calls an earlier attempt already
    made, so every mutating call says whether it changed anything.
    """

    series: SonarrSeries
    changed: bool
    # The seasons whose flag the write changed: a season that already had
    # the wanted flag is not the removal's to revert (#103).
    seasons: tuple[int, ...] = ()


@dataclass(frozen=True)
class ExclusionChange:
    """One import-exclusion call: the stored entry, and whether it created it (#103)."""

    exclusion: SonarrImportExclusion
    created: bool


def _apply_monitoring(
    resource: dict, monitored: bool, seasons: tuple[int, ...]
) -> bool:
    """Set the monitoring flags in ``resource``; report whether that changed it.

    With ``seasons`` only those seasons are touched, otherwise the series'
    own flag is. The resource is the one just read, so this is exactly the
    state the write asks for.
    """
    changed = False
    if not seasons:
        if bool(resource.get("monitored")) != monitored:
            resource["monitored"] = monitored
            changed = True
        return changed
    wanted = set(seasons)
    for season in resource.get("seasons", []):
        if season.get("seasonNumber") not in wanted:
            continue
        if bool(season.get("monitored")) != monitored:
            season["monitored"] = monitored
            changed = True
    return changed


class SonarrClient:
    """Talks to a Sonarr v3 server over its HTTP API.

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
        journal_size: int = DEFAULT_JOURNAL_SIZE,
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
        # Every outbound HTTP request, one entry per attempt: the exact
        # wire traffic of this client, observable beside what a server
        # saw (#69). Bounded to the most recent attempts (#101).
        self.requests: list[dict[str, str]] = RequestJournal(journal_size)

    def __repr__(self) -> str:
        return f"SonarrClient(base_url={self.base_url!r}, api_key=***)"

    @classmethod
    def from_settings(cls, settings, **overrides) -> "SonarrClient":
        """Build a client from the application settings."""
        options = {
            "base_url": settings.sonarr_base_url,
            "api_key": settings.sonarr_api_key,
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
            self.requests.append({"method": method, "path": path, "url": url})
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
                    "sonarr_unreachable",
                    extra={"method": method, "path": path, "attempts": attempt},
                )
                raise SonarrUnavailableError(
                    f"Sonarr could not be reached for {method} {path}: {error}"
                ) from None

            status = response.status_code
            if 300 <= status < 400:
                # Following a redirect would send the API key header to
                # whatever host the redirect names, so it is never followed.
                logger.warning(
                    "sonarr_redirect_refused",
                    extra={"method": method, "path": path, "status": status},
                )
                raise SonarrError(
                    f"Sonarr answered {status} with a redirect for {method} {path}; "
                    "refusing to follow it"
                )
            if status == 404:
                raise SonarrNotFound(f"Sonarr resource not found for {method} {path}")
            if status in (401, 403):
                logger.warning(
                    "sonarr_credentials_rejected",
                    extra={"method": method, "path": path},
                )
                raise SonarrAuthError(f"Sonarr rejected the API key for {method} {path}")
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "sonarr_error_status",
                    extra={"method": method, "path": path, "status": status},
                )
                raise SonarrUnavailableError(f"Sonarr returned {status} for {method} {path}")
            if status >= 400:
                logger.warning(
                    "sonarr_error_status",
                    extra={"method": method, "path": path, "status": status},
                )
                raise SonarrError(f"Sonarr returned {status} for {method} {path}")

            if not response.content:
                # A delete can answer 2xx with an empty body.
                return {}
            try:
                return response.json()
            except ValueError:
                raise SonarrError(
                    f"Sonarr returned an unreadable body for {method} {path}"
                ) from None

    # -- endpoints -------------------------------------------------------

    def system_status(self) -> SonarrSystemStatus:
        """The version and availability of the server."""
        payload = self._request("GET", f"{API_ROOT}/system/status")
        return SonarrSystemStatus.model_validate(payload)

    def all_series(self) -> list[SonarrSeries]:
        """Every series the server knows."""
        payload = self._request("GET", f"{API_ROOT}/series")
        return [SonarrSeries.model_validate(entry) for entry in payload]

    def series(self, sonarr_id: int) -> SonarrSeries:
        """One series by its Sonarr id."""
        payload = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        return SonarrSeries.model_validate(payload)

    def series_by_title(self, title: str) -> SonarrSeries | None:
        """The series with exactly this title, if the server has it."""
        wanted = title.strip().lower()
        for entry in self.all_series():
            if entry.title.strip().lower() == wanted:
                return entry
        return None

    def lookup_series(self, term: str) -> list[SonarrSeries]:
        """Search the server's index for a term."""
        payload = self._request(
            "GET", f"{API_ROOT}/series/lookup", params={"term": term}
        )
        return [SonarrSeries.model_validate(entry) for entry in payload]

    def delete_series(self, sonarr_id: int, delete_files: bool = True) -> None:
        """Delete a series and, by default, its files."""
        self._request(
            "DELETE",
            f"{API_ROOT}/series/{sonarr_id}",
            params={"deleteFiles": "true" if delete_files else "false"},
        )

    def series_monitored(self, sonarr_id: int) -> bool:
        """Whether the series is monitored right now (a read, never a write).

        The removal lifecycle reads this just before it changes the flag,
        so the effect it records is what the arr will really hold (#107).
        """
        payload = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        return bool(payload.get("monitored"))

    def monitored_seasons(
        self, sonarr_id: int, seasons: tuple[int, ...]
    ) -> tuple[int, ...]:
        """Which of these seasons the series monitors right now (a read).

        The season unmonitor records exactly the seasons it will flip, so a
        cancel re-monitors those and never a season the owner had already
        unmonitored (#103, #107).
        """
        payload = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        wanted = set(seasons)
        return tuple(
            season.get("seasonNumber")
            for season in payload.get("seasons", [])
            if season.get("seasonNumber") in wanted and bool(season.get("monitored"))
        )

    def unmonitor_series(self, sonarr_id: int) -> MonitoringChange:
        """Stop monitoring a series, writing only its monitoring flag (#102)."""
        resource = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        changed = _apply_monitoring(resource, False, ())
        if changed:
            self._write_monitoring(resource, False, ())
        return MonitoringChange(SonarrSeries.model_validate(resource), changed)

    def monitor_series(
        self, sonarr_id: int, seasons: tuple[int, ...] | None = None
    ) -> MonitoringChange:
        """Restore monitoring, writing only the monitoring it restores (#102).

        The reverse of :meth:`unmonitor_series` and
        :meth:`unmonitor_seasons`, used to undo the side effects of a
        removal that failed or was cancelled (#69). With ``seasons`` only
        those seasons are monitored again; without it the series itself
        is. Seasons the removal never touched keep their own state, and
        so does every field of the series: the write names monitoring
        only, never the whole resource the read returned.
        """
        wanted = tuple(seasons or ())
        resource = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        changed = _apply_monitoring(resource, True, wanted)
        if changed:
            self._write_monitoring(resource, True, wanted)
        return MonitoringChange(SonarrSeries.model_validate(resource), changed)

    def unmonitor_seasons(
        self, sonarr_id: int, seasons: tuple[int, ...]
    ) -> MonitoringChange:
        """Unmonitor only the selected seasons, writing only their flags (#102)."""
        wanted = tuple(seasons)
        resource = self._request("GET", f"{API_ROOT}/series/{sonarr_id}")
        before = {
            season.get("seasonNumber"): bool(season.get("monitored"))
            for season in resource.get("seasons", [])
        }
        changed = _apply_monitoring(resource, False, wanted)
        if changed:
            self._write_monitoring(resource, False, wanted)
        return MonitoringChange(
            SonarrSeries.model_validate(resource),
            changed,
            tuple(season for season in wanted if before.get(season)),
        )

    def _write_monitoring(
        self, resource: dict, monitored: bool, seasons: tuple[int, ...]
    ) -> None:
        """Write the monitoring flags, and nothing else (#102).

        ``PUT /series/{id}`` sends every field of the resource back, so it
        also writes back whatever the read saw: a value another client
        changed in between is lost. Sonarr accepts two narrower writes
        instead, each applied to the fields it names: the season pass for
        the seasons, and the series editor for the series' own flag.
        """
        if seasons:
            self._request(
                "POST",
                f"{API_ROOT}/seasonpass",
                json_body={
                    "series": [
                        {
                            "id": resource["id"],
                            "seasons": [
                                {"seasonNumber": number, "monitored": monitored}
                                for number in seasons
                            ],
                        }
                    ],
                },
                # No monitoringOptions: Sonarr then re-saves the series
                # without touching a single episode, which keeps this write
                # to the season flags named above.
            )
            return
        self._request(
            "PUT",
            f"{API_ROOT}/series/editor",
            json_body={"seriesIds": [resource["id"]], "monitored": monitored},
        )

    def episode_files(self, sonarr_id: int) -> list[SonarrEpisodeFile]:
        """List the series' episode files for exact season-scoped deletion."""
        payload = self._request(
            "GET", f"{API_ROOT}/episodefile", params={"seriesId": str(sonarr_id)}
        )
        return [SonarrEpisodeFile.model_validate(entry) for entry in payload]

    def delete_episode_files(self, file_ids: list[int]) -> None:
        """Delete only these episode files through Sonarr's bulk endpoint."""
        if file_ids:
            self._request(
                "DELETE", f"{API_ROOT}/episodefile/bulk",
                json_body={"episodeFileIds": file_ids},
            )

    def import_exclusions(self) -> list[SonarrImportExclusion]:
        """Every import exclusion Sonarr currently holds."""
        payload = self._request("GET", f"{API_ROOT}/importlistexclusion")
        return [SonarrImportExclusion.model_validate(entry) for entry in payload]

    def find_import_exclusion(self, tvdb_id: int) -> SonarrImportExclusion | None:
        """The exclusion the list holds for this series, if any (a read).

        The removal lifecycle reads this just before it posts an exclusion,
        so the effect it records is the entry it really creates (#107), and
        a cancel looks the recorded entry up again instead of trusting an
        arr id a crash may never have learned.
        """
        for existing in self.import_exclusions():
            if existing.tvdbId == tvdb_id:
                return existing
        return None

    def add_import_exclusion(self, tvdb_id: int, title: str) -> ExclusionChange:
        """Exclude the series from future imports so it is not downloaded again.

        The list is read first: Sonarr rejects a duplicate exclusion with a
        400, so a retry after a failed delete must reuse the entry a first
        attempt already stored instead of posting it again. An entry that
        was already there is returned with ``created=False``: the owner
        made it, so a cancel must leave it alone (#103).
        """
        existing = self.find_import_exclusion(tvdb_id)
        if existing is not None:
            return ExclusionChange(existing, created=False)
        payload = self._request(
            "POST",
            f"{API_ROOT}/importlistexclusion",
            json_body={"tvdbId": tvdb_id, "title": title},
        )
        return ExclusionChange(SonarrImportExclusion.model_validate(payload), created=True)

    def delete_import_exclusion(self, exclusion_id: int) -> None:
        """Remove one import exclusion by its id (#69).

        The reverse of :meth:`add_import_exclusion`, used when a removal
        is undone. A missing exclusion answers 404, which the caller
        reads as already reverted.
        """
        self._request("DELETE", f"{API_ROOT}/importlistexclusion/{exclusion_id}")

    def revert(self, effect) -> None:
        """Undo one recorded side effect this service owns (#69).

        ``effect`` is an ``AppliedEffect`` of the removal lifecycle
        (not imported here, to keep the dependency one-way): an
        ``unmonitor`` restores the series' monitoring — or exactly the
        seasons it stopped monitoring — and an ``exclusion`` deletes the
        stored import exclusion.

        Both are idempotent, so an effect recorded just before a call that
        a crash prevented, or that changed nothing, is harmless (#107): a
        series or season that is still monitored is not written at all, and
        an exclusion the list no longer holds is skipped instead of failing
        on an entry that is not there.
        """
        if effect.kind == "unmonitor":
            self.monitor_series(effect.resource_id, effect.seasons or None)
            return
        exclusion = self._recorded_exclusion(effect)
        if exclusion is None:
            logger.info(
                "sonarr_exclusion_already_reverted",
                extra={"tvdb_id": effect.external_id},
            )
            return
        self.delete_import_exclusion(exclusion.id)

    def _recorded_exclusion(self, effect) -> SonarrImportExclusion | None:
        """The exclusion this effect still owns, looked up by external id (#107).

        A record written before the POST may not know the arr id yet, and
        an id it does know can be stale: the list is the truth, so the
        entry is found by the series' tvdb id. Only a record that lacks it
        falls back to the stored id.
        """
        if effect.external_id is not None:
            return self.find_import_exclusion(effect.external_id)
        if effect.exclusion_id is None:
            return None
        for existing in self.import_exclusions():
            if existing.id == effect.exclusion_id:
                return existing
        return None
