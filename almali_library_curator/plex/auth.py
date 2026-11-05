"""The plex.tv sign-in flow: the PIN endpoints and the resources list (S13).

The PIN flow is the primary way to connect (L75). The app creates a PIN at
``POST https://plex.tv/api/v2/pins`` with the client headers, never with
``strong=true``: the app scans on a schedule and a seven-day JWT would
expire under it, so only the classic long-lived account token is accepted
(S13 rule 2). The owner authorizes at
``https://app.plex.tv/auth#?clientID=<id>&code=<code>`` and the app polls
its PIN until ``authToken`` appears.

The PIN code is not a credential and may be shown. The auth token is one:
it is sent as a header, never in a URL, and never logged (S13 rules 5-6).

Outbound rule (S13 rule 4 amending S9): this module is the only caller of
plex.tv, and it goes nowhere else.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from almali_library_curator.plex.client import CLIENT_IDENTIFIER

logger = logging.getLogger(__name__)

PLEX_TV_BASE_URL = "https://plex.tv"
AUTH_APP_URL = "https://app.plex.tv/auth"
PRODUCT_NAME = "Almali Library Curator for Plex"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RATE_LIMIT_PER_SECOND = 5.0

# Injected for tests; the throttle uses the same shape as the media client.
_monotonic = time.monotonic
_sleep = time.sleep


class PlexTvError(RuntimeError):
    """Any failure while talking to plex.tv. Never carries a token."""


class PlexTvUnavailableError(PlexTvError):
    """plex.tv could not be reached, or did not answer a usable response."""


class PlexTvRefusedError(PlexTvError):
    """plex.tv refused the request (a refused PIN or a refused token)."""


class PlexPinExpiredError(PlexTvError):
    """The PIN expired, or is unknown, before it was authorized."""


@dataclass(frozen=True)
class PlexPin:
    """A created PIN. The code is not a credential (S13 rule 5)."""

    id: str
    code: str


@dataclass(frozen=True)
class PlexServerOption:
    """One of the owner's own servers, with a local address (S13 rule 3)."""

    name: str
    address: str


class _Throttle:
    """The shared outbound rate limit the Plex client already uses."""

    def __init__(self, rate_limit_per_second: float) -> None:
        self.rate_limit_per_second = rate_limit_per_second
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        if self.rate_limit_per_second <= 0:
            return
        now = _monotonic()
        delay = self._next_allowed_at - now
        if delay > 0:
            _sleep(delay)
            now = _monotonic()
        self._next_allowed_at = now + (1.0 / self.rate_limit_per_second)


# One limiter for every plex.tv call: it protects plex.tv, not one client.
_throttle = _Throttle(DEFAULT_RATE_LIMIT_PER_SECOND)
_throttle_lock = threading.Lock()


def authorization_link(client_identifier: str, code: str) -> str:
    """The link the owner opens to authorize this app (S13 rule 1).

    It carries the client id and the PIN code only; never a token.
    """
    return f"{AUTH_APP_URL}#?clientID={quote(client_identifier, safe='')}&code={quote(code, safe='')}"


def _headers(*, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Product": PRODUCT_NAME,
    }
    if token:
        # The token travels as a header, never in the URL (S13 rule 6).
        headers["X-Plex-Token"] = token
    return headers


def _request(method: str, path: str, *, base_url: str | None = None,
             headers: dict[str, str] | None = None,
             timeout: float = DEFAULT_TIMEOUT_SECONDS,
             expired_statuses: frozenset[int] = frozenset()) -> object:
    base = (base_url or PLEX_TV_BASE_URL).rstrip("/")
    url = f"{base}{path}"
    with _throttle_lock:
        _throttle.wait()
    try:
        response = requests.request(
            method, url, headers=headers, timeout=timeout, allow_redirects=False
        )
    except requests.RequestException as error:
        # Class name only: an exception message can echo request internals.
        logger.warning("plex_tv_unreachable", extra={"path": path,
                                                     "error_type": type(error).__name__})
        raise PlexTvUnavailableError(
            f"plex.tv could not be reached for {path}: {type(error).__name__}"
        ) from None
    status = response.status_code
    if status in expired_statuses:
        # plex.tv answers 404/410 Gone for an expired or unknown PIN.
        logger.warning("plex_tv_pin_gone", extra={"path": path, "status": status})
        raise PlexPinExpiredError(f"the pin behind {path} expired at plex.tv")
    if 300 <= status < 400:
        # Following a redirect would re-send the token header to whatever
        # host the redirect names, so plex.tv redirects are never followed.
        logger.warning("plex_tv_redirect_refused", extra={"path": path, "status": status})
        raise PlexTvUnavailableError(
            f"plex.tv answered {status} with a redirect for {path}; refusing to follow it"
        )
    if status in (400, 401, 403):
        logger.warning("plex_tv_refused", extra={"path": path, "status": status})
        raise PlexTvRefusedError(f"plex.tv refused the request for {path} with {status}")
    if status == 429 or status >= 500:
        logger.warning("plex_tv_error_status", extra={"path": path, "status": status})
        raise PlexTvUnavailableError(f"plex.tv returned {status} for {path}")
    if status >= 400:
        logger.warning("plex_tv_error_status", extra={"path": path, "status": status})
        raise PlexTvError(f"plex.tv returned {status} for {path}")
    try:
        return response.json()
    except ValueError:
        logger.warning("plex_tv_unreadable_body", extra={"path": path})
        raise PlexTvUnavailableError(f"plex.tv returned an unreadable body for {path}") from None


def create_pin(*, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> PlexPin:
    """Create a PIN at plex.tv.

    The request carries ``X-Plex-Client-Identifier`` and ``X-Plex-Product``
    and no ``strong`` parameter, so the eventual token is the classic
    long-lived one (S13 rules 1-2).
    """
    payload = _request("POST", "/api/v2/pins", base_url=base_url,
                       headers=_headers(), timeout=timeout)
    if not isinstance(payload, dict):
        raise PlexTvUnavailableError("plex.tv returned an unexpected pin body")
    pin_id, code = payload.get("id"), payload.get("code")
    if pin_id in (None, "") or code in (None, ""):
        raise PlexTvUnavailableError("plex.tv returned a pin without an id or code")
    logger.info("plex_tv_pin_created")
    return PlexPin(str(pin_id), str(code))


def poll_pin(pin_id: str, *, base_url: str | None = None,
             timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """Poll the PIN. The auth token once the owner authorized, else None.

    An expired (or unknown) PIN raises its own error so the caller can show
    it instead of polling forever (S13 rule 4).
    """
    payload = _request("GET", f"/api/v2/pins/{pin_id}", base_url=base_url,
                       headers=_headers(), timeout=timeout,
                       expired_statuses=frozenset({404, 410}))
    if not isinstance(payload, dict):
        raise PlexTvUnavailableError("plex.tv returned an unexpected pin body")
    if payload.get("expired") is True or (payload.get("authToken") in (None, "")
                                          and "errors" in payload):
        raise PlexPinExpiredError(f"the pin {pin_id} expired at plex.tv")
    token = payload.get("authToken")
    return token if isinstance(token, str) and token else None


def list_resources(token: str, *, base_url: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[PlexServerOption]:
    """The owner's own servers with their local addresses (S13 rule 3).

    Shared servers and non-server resources are not offered: the owner
    picks an address of a server they own, and the app never writes an
    address the owner did not pick.
    """
    if not token:
        raise PlexTvRefusedError("a token is required to read the resource list")
    payload = _request("GET", "/api/v2/resources", base_url=base_url,
                       headers=_headers(token=token), timeout=timeout)
    if not isinstance(payload, list):
        raise PlexTvUnavailableError("plex.tv returned an unexpected resource body")
    options: list[PlexServerOption] = []
    seen: set[tuple[str, str]] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        provides = str(entry.get("provides") or "")
        if "server" not in provides.split(",") or entry.get("owned") is not True:
            continue
        name = str(entry.get("name") or "")
        connections = entry.get("connections") or []
        if not isinstance(connections, list):
            continue
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            address = connection.get("uri")
            if not connection.get("local") or not isinstance(address, str):
                continue
            if not address.startswith(("http://", "https://")):
                continue
            key = (name, address)
            if key not in seen:
                seen.add(key)
                options.append(PlexServerOption(name, address))
    logger.info("plex_tv_resources_listed", extra={"count": len(options)})
    return options
