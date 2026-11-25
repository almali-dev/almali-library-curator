"""Import attributed Tautulli history without inventing users or items."""
from __future__ import annotations

import logging
import sqlite3

from almali_library_curator.plex.models import is_watched
from almali_library_curator.plex.sync import epoch_to_iso
from almali_library_curator.tautulli import TautulliClient

logger = logging.getLogger(__name__)

#: How many unmatched Tautulli users the finished log names. A server can
#: know hundreds of accounts, so the record stays one bounded line and lists
#: the users whose plays were skipped the most (#127).
UNKNOWN_USER_LOG_LIMIT = 10


def import_tautulli_history(conn: sqlite3.Connection, tautulli: TautulliClient | None) -> int:
    """Return newly inserted events, counting unknown mappings in structured logs.

    The database's source-independent unique index also deduplicates events
    already supplied by Plex. Excluded accounts remain known; detection owns
    their exclusion from calculations. The whole history is asked for on
    every import (#112): Tautulli can store a play late with the date it
    happened at, so a window that started from the newest imported day would
    skip it for good. Asking for the whole history again inserts nothing
    twice, because the unique index refuses the rows it already holds.

    A play is attributed by its Tautulli user id and, when no account carries
    that id, by the Tautulli username compared case-insensitively with the
    account title (#127). A Tautulli user that matches neither is skipped, and
    the finished log names its heaviest skipped plays.
    """
    if tautulli is None:
        return 0
    entries = tautulli.history()
    users = {row["plex_user_id"]: row["id"] for row in conn.execute("SELECT id, plex_user_id FROM users")}
    titles: dict[str, int] = {}
    for row in conn.execute("SELECT id, title FROM users ORDER BY id"):
        titles.setdefault(row["title"].strip().casefold(), row["id"])
    keys = {row[0] for row in conn.execute(
        "SELECT plex_rating_key FROM media_items UNION SELECT plex_rating_key FROM episodes"
    )}
    imported = unknown_users = unknown_items = invalid = 0
    unknown_plays: dict[str, int] = {}
    with conn:
        for entry in entries:
            if entry.rating_key is None:
                # #119: a row that names no item cannot be stored.
                invalid += 1
                continue
            user_id = users.get(str(entry.user_id))
            if user_id is None and entry.user:
                # #127: Plex calls the owner 1 and Tautulli the owner's
                # plex.tv id, so the id alone cannot name the owner. The
                # Tautulli username is compared, case-insensitively, with the
                # account title; the id decides first when both could apply.
                user_id = titles.get(entry.user.strip().casefold())
            if user_id is None:
                unknown_users += 1
                name = (entry.user or "").strip()
                if name:
                    unknown_plays[name] = unknown_plays.get(name, 0) + 1
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
    logger.info("enrichment_history_finished", extra={
        "imported": imported, "skipped_unknown_users": unknown_users,
        "skipped_unknown_items": unknown_items, "skipped_invalid": invalid,
        "skipped_unknown_user_plays": dict(sorted(
            unknown_plays.items(), key=lambda item: (-item[1], item[0]))[:UNKNOWN_USER_LOG_LIMIT]),
    })
    return imported
