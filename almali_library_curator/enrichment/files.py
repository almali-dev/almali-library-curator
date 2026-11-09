"""Refresh movie and episode file facts without requiring host mounts."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from almali_library_curator import library_scope
from almali_library_curator.config import Settings
from almali_library_curator.hostfs import HostFilesystem

logger = logging.getLogger(__name__)

FILE_UPDATE_BATCH_SIZE = 1000


@dataclass
class FileSummary:
    movies: int = 0
    episodes: int = 0
    unavailable: int = 0


def _movie_query(allowed: frozenset[int] | None) -> tuple[str | None, tuple]:
    """Movies inside the curated scope, by item id (S14 rule 4)."""
    if allowed is None:
        return "SELECT id, file_path FROM media_items WHERE item_type='movie'", ()
    if not allowed:
        return None, ()
    marks = ", ".join("?" for _ in allowed)
    return (f"SELECT id, file_path FROM media_items WHERE item_type='movie' AND id IN ({marks})",
            tuple(sorted(allowed)))


def _episode_query(library_ids: tuple[int, ...] | None) -> tuple[str | None, tuple]:
    """Episodes inside the curated scope.

    The ``episodes`` table has no library column, so the scope is reached
    through ``media_item_id -> media_items.library_id``; an episode whose
    media item is unknown stays outside every scope.
    """
    if library_ids is None:
        return "SELECT id, file_path FROM episodes", ()
    if not library_ids:
        return None, ()
    marks = ", ".join("?" for _ in library_ids)
    return ("SELECT e.id, e.file_path FROM episodes e"
            " JOIN media_items m ON m.id = e.media_item_id"
            f" WHERE m.library_id IN ({marks})", tuple(library_ids))


def _update_batches(updates: list[tuple[int, int | None, int | None, str | None]]):
    """Yield compact JSON batches for the table-specific UPDATE statement.

    SQLite expands ordinary ``executemany`` rows into one traced UPDATE each.
    Packing rows keeps the executemany call while letting each traced UPDATE
    apply many file facts.
    """
    for offset in range(0, len(updates), FILE_UPDATE_BATCH_SIZE):
        rows = [
            {
                "id": row_id,
                "size": size_bytes,
                "hardlink": hardlink_count,
                "disk": disk,
            }
            for row_id, size_bytes, hardlink_count, disk in updates[
                offset:offset + FILE_UPDATE_BATCH_SIZE
            ]
        ]
        yield (json.dumps(rows, separators=(",", ":")),)


def _update_sql(table: str) -> str:
    """One UPDATE that applies every file fact in the supplied JSON batch."""
    return (
        f"UPDATE {table} SET "
        "file_size_bytes=COALESCE(json_extract(batch.value, '$.size'), "
        "file_size_bytes), "
        "hardlink_count=json_extract(batch.value, '$.hardlink'), "
        "disk=json_extract(batch.value, '$.disk') "
        "FROM json_each(?) AS batch "
        f"WHERE {table}.id=CAST(json_extract(batch.value, '$.id') AS INTEGER)"
    )


def refresh_files(conn: sqlite3.Connection, hostfs: HostFilesystem | None,
                  settings: Settings | None = None) -> FileSummary:
    """Keep Plex sizes when inspection is unavailable, but no assumed links/disk.

    Paths have already been mapped by PlexSync. They must not be mapped a
    second time here. Each inaccessible file degrades independently.

    Only items inside the curated scope are inspected (S14): a deselected
    library's files are never read. ``settings=None`` (or an empty curated
    selection) means no restriction, as today.
    """
    summary = FileSummary()
    allowed = library_scope.curated_item_ids(conn, settings) if settings is not None else None
    library_ids = library_scope.curated_library_ids(conn, settings) if settings is not None else None
    movie_sql, movie_params = _movie_query(allowed)
    episode_sql, episode_params = _episode_query(library_ids)
    with conn:
        for table, sql, params in (("media_items", movie_sql, movie_params),
                                   ("episodes", episode_sql, episode_params)):
            if sql is None:
                continue  # nothing of this kind is inside the curated scope
            rows = conn.execute(sql, params).fetchall()
            updates = []
            for row in rows:
                facts = None
                if hostfs is not None and row["file_path"]:
                    try:
                        facts = hostfs.file_facts(row["file_path"])
                    except Exception as error:
                        logger.warning("enrichment_file_failed", extra={"error_type": type(error).__name__})
                if table == "episodes":
                    summary.episodes += 1
                else:
                    summary.movies += 1
                if facts is None:
                    summary.unavailable += 1
                updates.append(
                    (row["id"],
                     facts.size_bytes if facts else None,
                     facts.hardlink_count if facts else None,
                     facts.disk if facts else None)
                )
            conn.executemany(_update_sql(table), _update_batches(updates))
    return summary
