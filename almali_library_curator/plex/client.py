"""Plex Media Server client."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from urllib.parse import urlencode

import requests

from almali_library_curator.plex.models import (
    PlexAccount, PlexHistoryEntry, PlexMetadata, PlexSection,
)

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


class PlexIdentityError(PlexError):
    """The address answered, but not with a Plex Media Server identity."""


class PlexNotFoundError(PlexError):
    """Plex does not know the requested item (HTTP 404)."""


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
        rate_limit_per_second: float = 20.0,
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
        expect_body: bool = True,
    ) -> dict:
        """One request with throttling, retries and the redirect refusal.

        ``expect_body=False`` accepts a success without a JSON body, which
        is how a real Plex answers a delete (200 and an empty body).
        """
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
                if status == 404:
                    raise PlexNotFoundError(f"Plex returned 404 for {path}")
                raise PlexError(f"Plex returned {status} for {path}")

            if not expect_body:
                try:
                    payload = response.json() if response.content.strip() else {}
                except ValueError:
                    payload = {}
                return payload if isinstance(payload, dict) else {}
            try:
                payload = response.json()
            except ValueError:
                raise PlexError(f"Plex returned an unreadable body for {path}") from None
            if not isinstance(payload, dict):
                raise PlexError(f"Plex returned an unexpected body for {path}")
            return payload

    def _paged(
        self, path: str, params: dict[str, str] | None = None, *,
        require_total: bool = False,
        model: type[PlexMetadata] = PlexMetadata,
    ) -> Iterator[list[PlexMetadata]]:
        """Read a collection one page of validated models at a time (#60).

        Nothing is accumulated: a consumer that stops after the first page
        stops the requests with it, so a library of any size is never held
        whole in memory.

        Never return a truncated section as a complete presence snapshot.
        Section/child reads require a stable total, matching page sizes and
        distinct rating keys. Other endpoints stop at ``totalSize`` when the
        server sends it and otherwise read until a page comes back short, so
        a server that omits the count cannot silently truncate the read.
        """
        start = 0
        expected_total: int | None = None
        seen: set[str] = set()
        previous_page: list[dict] | None = None
        while True:
            query = dict(params or {})
            query["X-Plex-Container-Start"] = str(start)
            query["X-Plex-Container-Size"] = str(self.page_size)
            container = self._request(path, query).get("MediaContainer")
            if not isinstance(container, dict):
                raise PlexError(f"Plex returned an incomplete collection for {path}")
            page = container.get("Metadata", [])
            if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
                raise PlexError(f"Plex returned an invalid collection for {path}")
            reported_total = container.get("totalSize")
            try:
                total = None if reported_total is None else int(reported_total)
                size = int(container.get("size", len(page)))
                offset = int(container.get("offset", start))
            except (TypeError, ValueError):
                raise PlexError(f"Plex returned an uncounted collection for {path}") from None
            if require_total:
                if total is None:
                    raise PlexError(f"Plex returned an uncounted collection for {path}")
                keys = [str(row.get("ratingKey", "")) for row in page]
                if (total < 0 or size != len(page) or offset != start
                        or (expected_total is not None and total != expected_total)
                        or start + len(page) > total
                        or (not page and start < total)
                        or any(not key or key in seen for key in keys)
                        or len(set(keys)) != len(keys)):
                    raise PlexError(f"Plex returned an incomplete collection for {path}")
                seen.update(keys)
                expected_total = total
            models = [model.model_validate(row) for row in page]
            yield models
            start += len(page)
            if not page:
                break
            if total is not None:
                if start >= total:
                    break
                continue
            # No totalSize: the read ends on a short page. A page that
            # repeats the previous one proves the server ignored the offset,
            # so the read refuses instead of asking for the same page forever.
            if page == previous_page:
                raise PlexError(f"Plex did not advance the page for {path}")
            previous_page = page
            if len(page) < self.page_size:
                break

    def _pages(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        require_total: bool = False,
        on_page: Callable[[list[PlexMetadata]], None] | None = None,
        model: type[PlexMetadata] = PlexMetadata,
    ) -> list[PlexMetadata] | None:
        """The pages of one collection, either as a list or as they arrive.

        With ``on_page`` every page is handed to the consumer while it is
        read and nothing is kept; without it the whole collection is
        returned as the list it always was.
        """
        pages = self._paged(
            path, params, require_total=require_total, model=model
        )
        if on_page is None:
            return [item for page in pages for item in page]
        for page in pages:
            on_page(page)
        return None

    # -- endpoints -------------------------------------------------------

    def server_identity(self) -> str:
        """The server's machine identifier.

        The root endpoint needs the token, so a valid answer proves both that
        the address is a Plex Media Server and that it accepts the token.
        """
        container = self._request("/").get("MediaContainer")
        identifier = container.get("machineIdentifier") if isinstance(container, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise PlexIdentityError("The address did not answer with a Plex server identity")
        return identifier

    def sections(self) -> list[PlexSection]:
        """The library sections of the server."""
        container = (self._request("/library/sections").get("MediaContainer") or {})
        return [PlexSection.model_validate(entry) for entry in container.get("Directory") or []]

    def section_items(
        self,
        section_key: str,
        item_type: str,
        include_guids: bool = True,
        on_page: Callable[[list[PlexMetadata]], None] | None = None,
    ) -> list[PlexMetadata] | None:
        """Every item of a section, read in pages.

        The external agent ids are only asked for when the caller needs them:
        the seasons and the episodes are read without them, so their stored
        rows keep the ids they always had. A page consumer receives the
        pages as they arrive instead of the whole list (#60).
        """
        code = TYPE_CODES.get(item_type, item_type)
        params = {"type": code}
        if include_guids:
            params["includeGuids"] = "1"
        return self._pages(
            f"/library/sections/{section_key}/all", params,
            require_total=True, on_page=on_page,
        )

    def children(
        self,
        rating_key: str,
        child_type: str,
        on_page: Callable[[list[PlexMetadata]], None] | None = None,
    ) -> list[PlexMetadata] | None:
        """The children of an item (seasons of a series, episodes of a season)."""
        code = TYPE_CODES.get(child_type, child_type)
        return self._pages(
            f"/library/metadata/{rating_key}/children", {"type": code},
            require_total=True, on_page=on_page,
        )

    def metadata(self, rating_key: str) -> PlexMetadata:
        """The metadata of a single item."""
        container = (self._request(f"/library/metadata/{rating_key}").get("MediaContainer") or {})
        entries = container.get("Metadata") or []
        if not entries:
            raise PlexError(f"Plex has no metadata for {rating_key}")
        return PlexMetadata.model_validate(entries[0])

    def media_files(self, rating_key: str) -> list[str]:
        """The distinct files of one item, read fresh from the server (S3).

        A live server answers each version as one ``Media`` entry carrying
        its own ``Part`` files, and marks none of them primary, so the set
        is what matters, never the order the entries came in. The deletion
        path uses this to refuse an item whose versions it cannot cover.
        """
        item = self.metadata(rating_key)
        return sorted({part.file for part in item.all_parts if part.file})

    def history(
        self,
        account_id: str | int | None = None,
        on_page: Callable[[list[PlexMetadata]], None] | None = None,
    ) -> list[PlexMetadata] | None:
        """The whole watch history, optionally for one account (#112).

        Every page is read on every call. The history is never cut short at
        the newest event already stored: Plex stores a play late with the
        instant it happened at, so a play older than the newest stored one
        can still arrive and must be read.
        """
        params: dict[str, str] = {}
        if account_id is not None:
            params["accountID"] = str(account_id)
        return self._pages(
            "/status/sessions/history/all", params or None, on_page=on_page,
            model=PlexHistoryEntry,
        )

    def accounts(self) -> list[PlexAccount]:
        """The accounts that can use the server."""
        container = (self._request("/accounts").get("MediaContainer") or {})
        return [PlexAccount.model_validate(entry) for entry in container.get("Account") or []]

    def collections(
        self,
        section_key: str,
        on_page: Callable[[list[PlexMetadata]], None] | None = None,
    ) -> list[PlexMetadata] | None:
        """The collections of a section."""
        return self._pages(
            f"/library/sections/{section_key}/collections", on_page=on_page
        )

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
            for entry in self.collections(section_key) or []
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
