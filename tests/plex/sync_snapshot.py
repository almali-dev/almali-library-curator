"""The snapshot of the rows the sync stores.

Shared by the tests that prove the sync stores the same data before and
after the optimisations of #3 and #10.
"""

from __future__ import annotations

import sqlite3

# Every table the sync writes.
SYNCED_TABLES = ("libraries", "media_items", "episodes", "users", "play_events")

# The two clock columns move with the wall clock; every other column is
# stored data and is part of a snapshot.
VOLATILE_COLUMNS = ("created_at", "updated_at")


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every row of every synced table, ordered by its own id."""
    tables: dict[str, list[tuple]] = {}
    for table in SYNCED_TABLES:
        columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
            if row["name"] not in VOLATILE_COLUMNS
        ]
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"
        ).fetchall()
        tables[table] = [tuple(row) for row in rows]
    return tables
