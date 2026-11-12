"""Plex Media Server integration."""

from almali_library_curator.plex.auth import (
    PlexPin,
    PlexPinExpiredError,
    PlexServerOption,
    PlexTvError,
    PlexTvRefusedError,
    PlexTvUnavailableError,
    authorization_link,
    create_pin,
    list_resources,
    poll_pin,
)
from almali_library_curator.plex.client import (
    PlexAuthError,
    PlexClient,
    PlexError,
    PlexNotFoundError,
    PlexUnavailableError,
)
from almali_library_curator.plex.deletion import (
    PlexDeletionRefused, PlexMultiVersionError, delete_media_item,
)
from almali_library_curator.plex.models import (
    PlexAccount,
    PlexMetadata,
    PlexSection,
    is_watched,
)
from almali_library_curator.plex.sync import PlexSync, SyncSummary

__all__ = [
    "PlexAccount",
    "PlexAuthError",
    "PlexClient",
    "PlexDeletionRefused",
    "PlexError",
    "PlexMetadata",
    "PlexMultiVersionError",
    "PlexNotFoundError",
    "PlexPin",
    "PlexPinExpiredError",
    "PlexSection",
    "PlexServerOption",
    "PlexSync",
    "PlexTvError",
    "PlexTvRefusedError",
    "PlexTvUnavailableError",
    "PlexUnavailableError",
    "SyncSummary",
    "authorization_link",
    "create_pin",
    "delete_media_item",
    "is_watched",
    "list_resources",
    "poll_pin",
]
