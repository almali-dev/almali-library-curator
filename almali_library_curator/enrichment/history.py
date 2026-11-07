"""Import attributed Tautulli history without inventing users or items."""
from __future__ import annotations

import logging
import sqlite3

from almali_library_curator.plex.models import is_watched
from almali_library_curator.plex.sync import epoch_to_iso
from almali_library_curator.tautulli import TautulliClient

logger = logging.getLogger(__name__)


def import_tautulli_history(conn: sqlite3.Connection, tautulli: TautulliClient | None) -> int:
    """Return newly inserted events, counting unknown mappings in structured logs.

    The database's source-independent unique index also deduplicates events
    already supplied by Plex. Excluded accounts remain known; detection owns
    their exclusion from calculations.
    """
    if tautulli is None:
        return 0
    entries = tautulli.history()
    users = {row["plex_user_id"]: row["id"] for row in conn.execute("SELECT id, plex_user_id FROM users")}
    keys = {row[0] for row in conn.execute(
        "SELECT plex_rating_key FROM media_items UNION SELECT plex_rating_key FROM episodes"
    )}
    imported = unknown_users = unknown_items = invalid = 0
    with conn:
        for entry in entries:
            user_id = users.get(str(entry.user_id))
            if user_id is None:
                unknown_users += 1
                continue
            key = str(entry.rating_key)
            if key not in keys:
                unknown_items += 1
                continue
            timestamp = entry.started if entry.started is not None else entry.date
            try:
                played_at = epoch_to_iso(timestamp)
            except (ValueError, OverflowError, OSError):
                played_at = None
            if played_at is None:
                invalid += 1
                continue
            duration = entry.duration or 0
            position = entry.view_offset
            if position is None:
                position = int(duration * (entry.percent_complete or 0) / 100)
            watched = is_watched(position, duration) or (entry.percent_complete or 0) >= 90
            cursor = conn.execute(
                "INSERT INTO play_events (user_id, plex_rating_key, watched, played_at, "
                "position_ms, duration_ms, source) VALUES (?, ?, ?, ?, ?, ?, 'tautulli') "
                "ON CONFLICT(user_id, plex_rating_key, played_at, position_ms) DO NOTHING",
                (user_id, key, int(watched), played_at, position, duration),
            )
            imported += cursor.rowcount
    logger.info("enrichment_history_finished", extra={
        "imported": imported, "skipped_unknown_users": unknown_users,
        "skipped_unknown_items": unknown_items, "skipped_invalid": invalid,
    })
    return imported
