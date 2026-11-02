"""Tautulli integration (optional source, S2)."""

from library_curator.tautulli.client import (
    TautulliAuthError,
    TautulliClient,
    TautulliError,
    TautulliUnavailableError,
)
from library_curator.tautulli.models import TautulliHistoryEntry, TautulliUser

__all__ = [
    "TautulliAuthError",
    "TautulliClient",
    "TautulliError",
    "TautulliHistoryEntry",
    "TautulliUnavailableError",
    "TautulliUser",
]
