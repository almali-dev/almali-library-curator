"""Plex Media Server integration."""

from library_curator.plex.client import (
    PlexAuthError,
    PlexClient,
    PlexError,
    PlexUnavailableError,
)
from library_curator.plex.models import (
    PlexAccount,
    PlexMetadata,
    PlexSection,
    is_watched,
)

__all__ = [
    "PlexAccount",
    "PlexAuthError",
    "PlexClient",
    "PlexError",
    "PlexMetadata",
    "PlexSection",
    "PlexUnavailableError",
    "is_watched",
]
