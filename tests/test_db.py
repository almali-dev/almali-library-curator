from __future__ import annotations

from almali_library_curator.db import SCHEMA_VERSION, MIGRATIONS, connect, init_db, migrate

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
    conn = init_db(tmp_path / "almali-library-curator.db")
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
    path = tmp_path / "almali-library-curator.db"

    init_db(path).close()
    conn = init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_v3_migration_is_additive_and_preserves_existing_requests(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Film', 'movie')")
    conn.execute("INSERT INTO removal_requests (media_item_id, category, reason, grace_days, due_at) VALUES (1, 'never_watched', 'Reason', 14, '2026-11-16')")
    conn.commit()
    assert migrate(conn) == 3
    row = conn.execute("SELECT * FROM removal_requests").fetchone()
    assert row["reason"] == "Reason"
    assert row["last_error"] is None
    assert row["deleted_at"] is None
    assert migrate(conn) == 3
    conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = init_db(tmp_path / "almali-library-curator.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
