"""Refresh movie and episode file facts without requiring host mounts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass

from almali_library_curator import library_scope
from almali_library_curator.config import Settings
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.enrichment.sources import record_disks

logger = logging.getLogger(__name__)

FILE_UPDATE_BATCH_SIZE = 1000

# The marker of the facts last recorded successfully, per row: the Plex path
# and the Plex-reported size the row carried when its facts were verified.
# app_meta already exists on fresh and upgraded installs (no migration, #58).
_FACTS_MARKER_KEY = 'file_facts_v1'
_MARKER_DIGEST_BYTES = 8


@dataclass
class FileSummary:
    movies: int = 0
    episodes: int = 0
    unavailable: int = 0


def _movie_query(allowed: frozenset[int] | None) -> tuple[str | None, tuple]:
    """Movies inside the curated scope, by item id (S14 rule 4).

    The Plex-reported size comes along: with the Plex path it is the marker
    that says whether the recorded facts still answer for the row (#58).
    """
    if allowed is None:
        return ("SELECT id, file_path, file_size_bytes FROM media_items"
                " WHERE item_type='movie' AND plex_present=1"), ()
    if not allowed:
        return None, ()
    marks = ", ".join("?" for _ in allowed)
    return ("SELECT id, file_path, file_size_bytes FROM media_items"
            f" WHERE item_type='movie' AND plex_present=1 AND id IN ({marks})",
            tuple(sorted(allowed)))


def _episode_query(library_ids: tuple[int, ...] | None) -> tuple[str | None, tuple]:
    """Episodes inside the curated scope.

    The ``episodes`` table has no library column, so the scope is reached
    through ``media_item_id -> media_items.library_id``; an episode whose
    media item is unknown stays outside every scope.
    """
    if library_ids is None:
        return "SELECT id, file_path, file_size_bytes FROM episodes WHERE plex_present=1", ()
    if not library_ids:
        return None, ()
    marks = ", ".join("?" for _ in library_ids)
    return ("SELECT e.id, e.file_path, e.file_size_bytes FROM episodes e"
            " JOIN media_items m ON m.id = e.media_item_id"
            f" WHERE e.plex_present=1 AND m.plex_present=1 AND m.library_id IN ({marks})", tuple(library_ids))


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


def _marker(path: str, size: int | None) -> str:
    """One row's marker: the Plex path (digested) and the Plex-reported size."""
    digest = hashlib.blake2b(
        os.fsencode(path), digest_size=_MARKER_DIGEST_BYTES
    ).hexdigest()
    return f"{size}:{digest}"


def _disk_layout(hostfs) -> list[str] | None:
    """The disk roots a refresh resolved under, or ``None`` when unknowable."""
    roots = getattr(hostfs, "disk_roots", None)
    if roots is None:
        return None
    try:
        return sorted(roots())
    except Exception:  # an adapter of another shape keeps its own answers
        return None


@dataclass
class _Markers:
    """The markers of the last refresh, with the disk layout it ran under."""

    rows: dict[str, dict[str, str]]
    layout: list[str] | None = None
    stored: str | None = None


def _load_markers(conn: sqlite3.Connection) -> _Markers:
    """Read the markers recorded by the last refresh, and the stored text.

    Unreadable markers are treated as absent: a refresh then inspects every
    file, which is what this build did before the markers existed.
    """
    row = conn.execute(
        'SELECT value FROM app_meta WHERE key=?', (_FACTS_MARKER_KEY,)
    ).fetchone()
    if row is None:
        return _Markers({})
    try:
        value = json.loads(row[0])
    except ValueError:
        return _Markers({})
    if not isinstance(value, dict):
        return _Markers({})
    rows, layout = value.get("rows"), value.get("roots")
    if (not isinstance(rows, dict)
            or not all(isinstance(entries, dict) for entries in rows.values())
            or not all(isinstance(v, str) for entries in rows.values()
                       for v in entries.values())
            or not (layout is None or (isinstance(layout, list)
                                       and all(isinstance(name, str) for name in layout)))):
        return _Markers({})
    return _Markers({table: dict(entries) for table, entries in rows.items()},
                    layout, row[0])


def _store_markers(conn: sqlite3.Connection, markers: _Markers) -> None:
    """Record the markers atomically with the facts they describe."""
    if not markers.rows and markers.stored is None:
        return  # nothing was ever recorded: do not write an empty marker
    value = json.dumps({"roots": markers.layout, "rows": markers.rows},
                       sort_keys=True, separators=(",", ":"))
    if value == markers.stored:
        return  # no row needed a new answer: no write at all
    conn.execute('INSERT INTO app_meta (key, value) VALUES (?, ?)'
                 ' ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (_FACTS_MARKER_KEY, value))


def refresh_files(conn: sqlite3.Connection, hostfs: HostFilesystem | None,
                  settings: Settings | None = None) -> FileSummary:
    """Keep Plex sizes when inspection is unavailable, but no assumed links/disk.

    Paths have already been mapped by PlexSync. They must not be mapped a
    second time here. Each inaccessible file degrades independently.

    Only items inside the curated scope are inspected (S14): a deselected
    library's files are never read. ``settings=None`` (or an empty curated
    selection) means no restriction, as today.

    A row whose Plex path and Plex-reported size are unchanged since its facts
    were last recorded is not inspected at all, and the files of one directory
    share the disk resolved for the first of them (#58). Every other case --
    a new or changed row, a failed probe, a degraded adapter -- reads the file
    as before, so a changed path, a changed size or a vanished file is still
    found on the refresh that Plex reports it.
    """
    summary = FileSummary()
    allowed = library_scope.curated_item_ids(conn, settings) if settings is not None else None
    library_ids = library_scope.curated_library_ids(conn, settings) if settings is not None else None
    movie_sql, movie_params = _movie_query(allowed)
    episode_sql, episode_params = _episode_query(library_ids)
    markers = _load_markers(conn)
    layout = _disk_layout(hostfs)
    if markers.layout is not None and layout is not None and markers.layout != layout:
        # The disks changed since the facts were recorded (a mount appeared or
        # vanished): no recorded disk answer is trusted, every file is read.
        markers.rows = {}
    if layout is not None:
        # An adapter that cannot report its roots leaves the known layout alone.
        markers.layout = layout
    # A test double or a degraded adapter without a session still refreshes;
    # it simply resolves every file's disk on its own.
    session = getattr(hostfs, "refresh_session", nullcontext)
    with conn, session():
        skipped = probed = 0
        for table, sql, params in (("media_items", movie_sql, movie_params),
                                   ("episodes", episode_sql, episode_params)):
            if sql is None:
                continue  # nothing of this kind is inside the curated scope
            table_markers = markers.rows.setdefault(table, {})
            rows = conn.execute(sql, params).fetchall()
            updates = []
            for row in rows:
                if table == "episodes":
                    summary.episodes += 1
                else:
                    summary.movies += 1
                row_key = str(row["id"])
                path = row["file_path"]
                marker = _marker(path, row["file_size_bytes"]) if path else None
                if (hostfs is not None and marker is not None
                        and table_markers.get(row_key) == marker):
                    # Unchanged Plex data: the recorded facts still answer.
                    skipped += 1
                    continue
                probed += 1
                facts = None
                if hostfs is not None and path:
                    try:
                        facts = hostfs.file_facts(path)
                    except Exception as error:
                        logger.warning("enrichment_file_failed", extra={"error_type": type(error).__name__})
                if facts is None:
                    summary.unavailable += 1
                    # Facts were not recorded successfully: probe again next time.
                    table_markers.pop(row_key, None)
                elif marker is not None:
                    table_markers[row_key] = marker
                updates.append(
                    (row["id"],
                     facts.size_bytes if facts else None,
                     facts.hardlink_count if facts else None,
                     facts.disk if facts else None)
                )
            conn.executemany(_update_sql(table), _update_batches(updates))
        _store_markers(conn, markers)
        record_disks(conn)
    logger.info("enrichment_files_refreshed", extra={"probed": probed, "skipped": skipped})
    return summary
