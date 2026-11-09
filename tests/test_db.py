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
    assert migrate(conn) == SCHEMA_VERSION
    row = conn.execute("SELECT * FROM removal_requests").fetchone()
    assert row["reason"] == "Reason"
    assert row["last_error"] is None
    assert row["deleted_at"] is None
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v4_migration_adds_external_ids_and_scan_reports(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:3]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Film', 'movie')")
    conn.execute("INSERT INTO scan_runs (status) VALUES ('finished')")
    conn.commit()

    assert migrate(conn) == SCHEMA_VERSION

    item = conn.execute("SELECT * FROM media_items").fetchone()
    assert item["title"] == "Film"
    assert (item["tmdb_id"], item["tvdb_id"], item["imdb_id"]) == (None, None, None)
    run = conn.execute("SELECT * FROM scan_runs").fetchone()
    assert run["status"] == "finished"
    assert (run["trigger"], run["report"]) == (None, None)
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v5_migration_adds_release_year_and_episode_file_facts(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:4]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Show', 'series')")
    conn.execute("INSERT INTO episodes (plex_rating_key, series_rating_key, file_size_bytes) VALUES ('e1', '1', 42)")
    conn.commit()

    assert migrate(conn) == SCHEMA_VERSION

    assert conn.execute("SELECT year FROM media_items").fetchone()["year"] is None
    episode = conn.execute("SELECT * FROM episodes").fetchone()
    assert episode["file_size_bytes"] == 42
    assert (episode["disk"], episode["hardlink_count"]) == (None, None)
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v6_migration_adds_the_scheduler_slot_index_without_changing_runs(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:5]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 5")
    conn.execute(
        "INSERT INTO scan_runs (started_at, status, trigger) "
        "VALUES ('2026-11-02T06:00:00+00:00', 'finished', 'schedule')"
    )
    conn.commit()

    assert migrate(conn) == 6

    indexes = {
        row["name"]
        for row in conn.execute("PRAGMA index_list('scan_runs')")
    }
    assert "idx_scan_runs_schedule_slot" in indexes
    run = conn.execute("SELECT status, trigger FROM scan_runs").fetchone()
    assert (run["status"], run["trigger"]) == ("finished", "schedule")
    assert migrate(conn) == 6
    conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = init_db(tmp_path / "almali-library-curator.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
