"""Refresh movie and episode file facts without requiring host mounts."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from almali_library_curator.hostfs import HostFilesystem

logger = logging.getLogger(__name__)


@dataclass
class FileSummary:
    movies: int = 0
    episodes: int = 0
    unavailable: int = 0


def refresh_files(conn: sqlite3.Connection, hostfs: HostFilesystem | None) -> FileSummary:
    """Keep Plex sizes when inspection is unavailable, but no assumed links/disk.

    Paths have already been mapped by PlexSync. They must not be mapped a
    second time here. Each inaccessible file degrades independently.
    """
    summary = FileSummary()
    with conn:
        for table, condition in (("media_items", "item_type='movie'"), ("episodes", "1=1")):
            rows = conn.execute(f"SELECT id, file_path FROM {table} WHERE {condition}").fetchall()
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
                conn.execute(
                    f"UPDATE {table} SET file_size_bytes=COALESCE(?, file_size_bytes), "
                    "hardlink_count=?, disk=? WHERE id=?",
                    (facts.size_bytes if facts else None,
                     facts.hardlink_count if facts else None,
                     facts.disk if facts else None, row["id"]),
                )
    return summary
