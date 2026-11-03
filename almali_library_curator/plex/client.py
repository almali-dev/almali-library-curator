"""Plex Media Server client."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlencode

import requests

from almali_library_curator.plex.models import PlexAccount, PlexMetadata, PlexSection

logger = logging.getLogger(__name__)

CLIENT_IDENTIFIER = "almali-library-curator"

# Plex type codes used by the item endpoints.
TYPE_CODES = {"movie": "1", "show": "2", "season": "3", "episode": "4"}

# The list-then-create sequence of a collection is serialised process-wide,
# so two callers never create two collections with the same name.
_COLLECTION_LOCK = threading.Lock()


class PlexError(RuntimeError):
    """Any failure while talking to Plex."""


class PlexAuthError(PlexError):
    """Plex rejected the credentials."""


class PlexUnavailableError(PlexError):
    """Plex could not be reached, or kept failing."""


class PlexClient:
    """Reads a Plex Media Server over its HTTP API.

    Every collection is read in pages, every outbound request is rate limited,
    and the token never reaches a log record or an error message.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 15.0,
        rate_limit_per_second: float = 5.0,
        page_size: int = 200,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        path_map: Sequence[tuple[str, str]] = (),
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.rate_limit_per_second = rate_limit_per_second
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.path_map = tuple((str(source), str(target)) for source, target in path_map)
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._next_allowed_at = 0.0

    def __repr__(self) -> str:
        return f"PlexClient(base_url={self.base_url!r}, token=***)"

    @classmethod
    def from_settings(cls, settings, **overrides) -> "PlexClient":
        """Build a client from the application settings."""
        options = {
            "base_url": settings.plex_base_url,
            "token": settings.plex_token,
            "timeout": settings.plex_timeout_seconds,
            "rate_limit_per_second": settings.plex_rate_limit_per_second,
            "page_size": settings.plex_page_size,
            "path_map": settings.plex_path_map,
        }
        options.update(overrides)
        return cls(**options)

    # -- configuration ---------------------------------------------------

    def map_path(self, path: str) -> str:
        """Map a Plex-reported path onto the path the app can read."""
        for source, target in self.path_map:
            if path == source:
                return target
            if path.startswith(source.rstrip("/") + "/"):
                return target.rstrip("/") + path[len(source.rstrip("/")) :]
        return path

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Token": self._token,
            "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
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
        path: str,
        params: dict[str, str] | None = None,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        query = dict(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        attempt = 0
        limit = self.max_retries if max_attempts is None else max_attempts
        while True:
            self._throttle()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                    allow_redirects=False,
                    json=json_body,
                )
            except requests.RequestException as error:
                if attempt < limit:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "plex_unreachable", extra={"path": path, "attempts": attempt}
                )
                raise PlexUnavailableError(
                    f"Plex could not be reached for {path}: {error}"
                ) from None

            status = response.status_code
            if 300 <= status < 400:
                # Following a redirect would send the token header to whatever
                # host the redirect names, so the client never follows one.
                logger.warning("plex_redirect_refused", extra={"path": path, "status": status})
                raise PlexError(
                    f"Plex answered {status} with a redirect for {path}; "
                    "refusing to follow it"
                )
            if status in (401, 403):
                logger.warning("plex_credentials_rejected", extra={"path": path})
                raise PlexAuthError(f"Plex rejected the credentials for {path}")
            if status == 429 or status >= 500:
                if attempt < limit:
                    attempt += 1
                    self._backoff(attempt)
                    continue
                logger.warning(
                    "plex_error_status", extra={"path": path, "status": status}
                )
                raise PlexUnavailableError(f"Plex returned {status} for {path}")
            if status >= 400:
                logger.warning("plex_error_status", extra={"path": path, "status": status})
                raise PlexError(f"Plex returned {status} for {path}")

            try:
                payload = response.json()
            except ValueError:
                raise PlexError(f"Plex returned an unreadable body for {path}") from None
            if not isinstance(payload, dict):
                raise PlexError(f"Plex returned an unexpected body for {path}")
            return payload

    def _paged(self, path: str, params: dict[str, str] | None = None) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            query = dict(params or {})
            query["X-Plex-Container-Start"] = str(start)
            query["X-Plex-Container-Size"] = str(self.page_size)
            container = (self._request(path, query).get("MediaContainer") or {})
            page = container.get("Metadata") or []
            items.extend(page)
            total = container.get("totalSize", container.get("size", len(items)))
            start += len(page)
            if not page or start >= int(total):
                break
        return items

    # -- endpoints -------------------------------------------------------

    def sections(self) -> list[PlexSection]:
        """The library sections of the server."""
        container = (self._request("/library/sections").get("MediaContainer") or {})
        return [PlexSection.model_validate(entry) for entry in container.get("Directory") or []]

    def section_items(self, section_key: str, item_type: str) -> list[PlexMetadata]:
        """Every item of a section, read in pages."""
        code = TYPE_CODES.get(item_type, item_type)
        raw = self._paged(f"/library/sections/{section_key}/all", {"type": code})
        return [PlexMetadata.model_validate(entry) for entry in raw]

    def children(self, rating_key: str, child_type: str) -> list[PlexMetadata]:
        """The children of an item (seasons of a series, episodes of a season)."""
        code = TYPE_CODES.get(child_type, child_type)
        raw = self._paged(f"/library/metadata/{rating_key}/children", {"type": code})
        return [PlexMetadata.model_validate(entry) for entry in raw]

    def metadata(self, rating_key: str) -> PlexMetadata:
        """The metadata of a single item."""
        container = (self._request(f"/library/metadata/{rating_key}").get("MediaContainer") or {})
        entries = container.get("Metadata") or []
        if not entries:
            raise PlexError(f"Plex has no metadata for {rating_key}")
        return PlexMetadata.model_validate(entries[0])

    def history(self, account_id: str | int | None = None) -> list[PlexMetadata]:
        """The watch history, optionally for one account, read in pages."""
        params = {"accountID": str(account_id)} if account_id is not None else None
        raw = self._paged("/status/sessions/history/all", params)
        return [PlexMetadata.model_validate(entry) for entry in raw]

    def accounts(self) -> list[PlexAccount]:
        """The accounts that can use the server."""
        container = (self._request("/accounts").get("MediaContainer") or {})
        return [PlexAccount.model_validate(entry) for entry in container.get("Account") or []]

    def collections(self, section_key: str) -> list[PlexMetadata]:
        """The collections of a section."""
        raw = self._paged(f"/library/sections/{section_key}/collections")
        return [PlexMetadata.model_validate(entry) for entry in raw]

    def ensure_collection(self, section_key: str, name: str) -> str:
        """The rating key of the named collection, creating it if missing.

        The name is matched exactly; only a missing collection is created,
        so the collection is never duplicated (S5 step 3).
        """
        with _COLLECTION_LOCK:
            return self._ensure_collection(section_key, name)

    def _ensure_collection(self, section_key: str, name: str) -> str:
        existing = self._matching_collections(section_key, name)
        if existing:
            return existing[0]
        attempt = 0
        while True:
            try:
                container = (
                    self._request(
                        f"/library/sections/{section_key}/collections",
                        method="POST",
                        json_body={"title": name},
                        max_attempts=0,
                    ).get("MediaContainer")
                    or {}
                )
                break
            except PlexError:
            # The creation may have been applied even though its answer was
            # lost; a blind retry would create a second collection, so the
            # existence is re-checked before trying again.
                existing = self._matching_collections(section_key, name)
                if existing:
                    return existing[0]
                if attempt >= self.max_retries:
                    raise
                attempt += 1
        # After the creation the section is read again and the lowest
        # matching rating key wins, so concurrent callers converge on the
        # same collection instead of splitting the memberships.
        existing = self._matching_collections(section_key, name)
        if existing:
            return existing[0]
        entries = container.get("Metadata") or []
        if not entries or not entries[0].get("ratingKey"):
            raise PlexError(f"Plex did not return the created collection for {name}")
        return str(entries[0]["ratingKey"])

    def _matching_collections(self, section_key: str, name: str) -> list[str]:
        """The rating keys of the collections named ``name``, lowest first."""
        keys = [
            entry.ratingKey
            for entry in self.collections(section_key)
            if entry.title == name
        ]
        return sorted(
            keys, key=lambda key: (0, int(key)) if key.isdigit() else (1, 0)
        )

    def add_to_collection(self, collection_key: str, rating_key: str) -> None:
        """Put one item in a collection."""
        self._request(
            f"/library/collections/{collection_key}/items/{rating_key}",
            method="PUT",
        )

    def remove_from_collection(self, collection_key: str, rating_key: str) -> None:
        """Take one item out of a collection."""
        self._request(
            f"/library/collections/{collection_key}/items/{rating_key}",
            method="DELETE",
        )
