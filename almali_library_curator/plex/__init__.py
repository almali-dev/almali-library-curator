"""Plex Media Server integration."""

from almali_library_curator.plex.client import (
    PlexAuthError,
    PlexClient,
    PlexError,
    PlexUnavailableError,
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
    "PlexError",
    "PlexMetadata",
    "PlexSection",
    "PlexSync",
    "PlexUnavailableError",
    "SyncSummary",
    "is_watched",
]
