"""The curated-library scope (S14). The rule lives here, asked everywhere.

``Settings.curated_libraries`` holds Plex library keys. **An empty selection
means every library is curated** (no restriction), so a fresh install and
every install upgraded from before the setting existed keep curating
everything; a non-empty selection restricts the app to exactly those keys.

The sync, the detection engine, the lifecycle engine and the web views all
ask this module, so the rule exists once and is never re-implemented.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

from almali_library_curator.config import Settings


class _HasCuratedLibraries(Protocol):
    """The one settings attribute the scope rule needs."""

    curated_libraries: tuple[str, ...]


def curated_library_keys(settings: _HasCuratedLibraries) -> tuple[str, ...]:
    """The curated Plex library keys, stripped; empty means every library."""
    return tuple(key.strip() for key in settings.curated_libraries if str(key).strip())


def library_is_curated(plex_key: str, curated: tuple[str, ...]) -> bool:
    """S14 rule 1: an empty selection curates every library."""
    keys = tuple(key.strip() for key in curated if str(key).strip())
    return not keys or plex_key in keys


def curated_library_ids(
    conn: sqlite3.Connection, settings: Settings
) -> tuple[int, ...] | None:
    """Database ids of the curated libraries.

    ``None`` means there is no restriction: every library is curated.
    An empty tuple means a restriction that matches no synced library,
    so nothing is inside the scope.
    """
    keys = curated_library_keys(settings)
    if not keys:
        return None
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT id FROM libraries WHERE plex_key IN ({placeholders})", keys
    )
    return tuple(int(row["id"]) for row in rows)


def curated_item_ids(
    conn: sqlite3.Connection, settings: Settings
) -> frozenset[int] | None:
    """Media item ids inside the curated libraries.

    Proven-absent Plex items and deselected libraries stay in storage but
    are outside this set. ``None`` means every stored item is eligible.
    """
    ids = curated_library_ids(conn, settings)
    if ids is None:
        if conn.execute("SELECT 1 FROM media_items WHERE plex_present = 0 LIMIT 1").fetchone() is None:
            return None
        rows = conn.execute("SELECT id FROM media_items WHERE plex_present = 1")
    else:
        if not ids:
            return frozenset()
        placeholders = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM media_items WHERE plex_present = 1 AND library_id IN ({placeholders})", ids
        )
    return frozenset(int(row["id"]) for row in rows)


def item_is_curated(
    conn: sqlite3.Connection, settings: Settings, media_item_id: int
) -> bool:
    """Whether the item's library is inside the scope (S14 rule 6).

    An item without a synced library row is outside every scope: nothing
    unsynced may be marked or deleted.
    """
    row = conn.execute(
        "SELECT l.plex_key FROM media_items mi"
        " JOIN libraries l ON l.id = mi.library_id WHERE mi.id = ? AND mi.plex_present = 1",
        (media_item_id,),
    ).fetchone()
    if row is None:
        return False
    return library_is_curated(row["plex_key"], curated_library_keys(settings))
