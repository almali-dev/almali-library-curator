"""Import attributed Tautulli history without inventing users or items."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from almali_library_curator.db import meta_value, set_meta
from almali_library_curator.plex.models import is_watched
from almali_library_curator.plex.sync import epoch_to_iso
from almali_library_curator.tautulli import TautulliClient

logger = logging.getLogger(__name__)

# The day of the newest play this database imported from Tautulli (#74).
# Tautulli's own ``after`` filter takes a date, so the next import asks for
# that day onwards instead of the whole history.
HISTORY_CURSOR_KEY = "tautulli_history_date"

# Tautulli's date filter cannot see inside a day, and a play can be stored
# late, so the window starts one day before the cursor. The stored unique
# index deduplicates whatever the overlap returns.
CURSOR_OVERLAP_DAYS = 1


def _window_start(cursor: str | None) -> str | None:
    """The ``after`` date for a stored cursor, or None for the full history."""
    if cursor is None:
        return None
    try:
        newest = datetime.fromisoformat(cursor).date()
    except ValueError:
        return None
    return (newest - timedelta(days=CURSOR_OVERLAP_DAYS)).isoformat()


def _entry_day(timestamp: int | None) -> str | None:
    """The UTC day of one Tautulli timestamp."""
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def import_tautulli_history(conn: sqlite3.Connection, tautulli: TautulliClient | None) -> int:
    """Return newly inserted events, counting unknown mappings in structured logs.

    The database's source-independent unique index also deduplicates events
    already supplied by Plex. Excluded accounts remain known; detection owns
    their exclusion from calculations. Only the days since the last import
    are asked for (#74), and the cursor advances in the same transaction that
    stores the events it covers: a failed import leaves it where it was.
    """
    if tautulli is None:
        return 0
    stored = meta_value(conn, HISTORY_CURSOR_KEY)
    after = _window_start(stored)
    entries = tautulli.history(after=after)
    users = {row["plex_user_id"]: row["id"] for row in conn.execute("SELECT id, plex_user_id FROM users")}
    keys = {row[0] for row in conn.execute(
        "SELECT plex_rating_key FROM media_items UNION SELECT plex_rating_key FROM episodes"
    )}
    imported = unknown_users = unknown_items = invalid = 0
    newest = stored
    with conn:
        for entry in entries:
            # The window is what was read, never what could be stored: an
            # entry of an unknown user or item still moves the cursor, or the
            # next import would ask for the same days forever.
            day = _entry_day(entry.started if entry.started is not None else entry.date)
            if day is not None and (newest is None or day > newest):
                newest = day
            if entry.rating_key is None:
                # #119: a row that names no item cannot be stored.
                invalid += 1
                continue
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
            result = conn.execute(
                "INSERT INTO play_events (user_id, plex_rating_key, watched, played_at, "
                "position_ms, duration_ms, source) VALUES (?, ?, ?, ?, ?, ?, 'tautulli') "
                "ON CONFLICT(user_id, plex_rating_key, played_at, position_ms) DO NOTHING",
                (user_id, key, int(watched), played_at, position, duration),
            )
            imported += result.rowcount
        if newest is not None and newest != stored:
            set_meta(conn, HISTORY_CURSOR_KEY, newest)
    logger.info("enrichment_history_finished", extra={
        "imported": imported, "skipped_unknown_users": unknown_users,
        "skipped_unknown_items": unknown_items, "skipped_invalid": invalid,
        "window_start": after,
    })
    return imported
