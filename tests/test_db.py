from __future__ import annotations

from library_curator.db import SCHEMA_VERSION, init_db

EXPECTED_TABLES = {
    "libraries",
    "media_items",
    "episodes",
    "users",
    "play_events",
    "scan_runs",
    "candidates",
    "protections",
    "removal_requests",
    "removal_events",
    "app_meta",
}


def test_init_db_creates_the_core_schema(tmp_path):
    conn = init_db(tmp_path / "library-curator.db")
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "library-curator.db"

    init_db(path).close()
    conn = init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = init_db(tmp_path / "library-curator.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
