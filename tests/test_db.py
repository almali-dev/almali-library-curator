from __future__ import annotations

import sqlite3

import pytest

from almali_library_curator import db as db_module
from almali_library_curator.db import (
    SCHEMA_VERSION,
    MIGRATIONS,
    SchemaVersionError,
    connect,
    init_db,
    migrate,
)

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


def test_connection_waits_thirty_seconds_for_a_busy_writer(tmp_path):
    conn = init_db(tmp_path / 'timeout.db')
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30_000
    conn.close()


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


def test_v11_upgrades_legacy_reports_once_and_tolerates_corrupt_json(tmp_path):
    import json
    from almali_library_curator.scan import last_scan
    from almali_library_curator.web.views import scan_status_view
    path = tmp_path / 'legacy.db'
    conn = connect(path)
    for script in MIGRATIONS[:9]:
        conn.executescript(script)
    conn.execute('PRAGMA user_version = 9')
    for number in range(35):
        body = json.dumps({'sync': {'status': 'finished'}, 'padding': 'x' * 10_000})
        conn.execute('INSERT INTO scan_runs (status, report) VALUES (?, ?)',
                     ('finished' if number == 0 else 'failed', body))
    conn.execute("UPDATE scan_runs SET report='broken' WHERE id=35")
    conn.commit()
    conn.close()
    conn = init_db(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert conn.execute('SELECT COUNT(*) FROM scan_runs WHERE report IS NOT NULL').fetchone()[0] == 30
    assert last_scan(conn)['id'] == 1 and 'report' in last_scan(conn)
    assert conn.execute('SELECT steps FROM scan_run_views WHERE run_id=1').fetchone() is not None
    assert scan_status_view(conn).state == 'failed'
    assert all(step.status == 'pending' for step in scan_status_view(conn).steps)
    conn.close()
    assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
    init_db(path).close()  # idempotent; no second migration backup
    assert len(list((tmp_path / 'backups').glob('*.db'))) == 1


def test_v10_failed_migration_restores_reports_and_has_no_partial_projections(tmp_path, monkeypatch):
    conn = connect(tmp_path / 'legacy.db')
    for script in MIGRATIONS[:9]:
        conn.executescript(script)
    conn.execute('PRAGMA user_version = 9')
    for _ in range(35):
        conn.execute("INSERT INTO scan_runs (report) VALUES ('{}')")
    conn.commit()
    monkeypatch.setattr(db_module, 'MIGRATIONS', MIGRATIONS[:9] + (MIGRATIONS[9] + 'INVALID SQL;',))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
    assert conn.execute('SELECT COUNT(*) FROM scan_runs WHERE report IS NOT NULL').fetchone()[0] == 35
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'scan_run_view%'").fetchall() == []
    conn.close()


# The previous build (schema 10) projected only the two row sections, at the
# top level of the snapshot. A database it built still has that shape.
LEGACY_V10_PROJECTION = """
    WITH valid AS (
        SELECT id, CASE WHEN json_valid(report) THEN report ELSE '{}' END AS body
        FROM scan_runs
    )
    SELECT id,
        json_object('candidate_rows', json_extract(body, '$.detection.counts.candidate_rows'),
                    'protected_rows', json_extract(body, '$.detection.counts.protected_rows')),
        json_object('sync', json_extract(body, '$.sync.status'),
                    'enrichment', json_extract(body, '$.enrichment.status'),
                    'detection', json_extract(body, '$.detection.status'),
                    'recheck', json_extract(body, '$.recheck.status'),
                    'deletion', json_extract(body, '$.deletion.status'))
    FROM valid
"""


def test_v11_upgrade_keeps_the_display_of_a_schema_10_database(tmp_path):
    """A database built by the previous build still shows its rows."""
    import json
    from almali_library_curator.web.views import candidates_view, report_section_rows
    path = tmp_path / 'v10.db'
    conn = connect(path)
    for script in MIGRATIONS[:9]:
        conn.executescript(script)
    conn.execute('PRAGMA user_version = 9')
    row = dict(media_item_id=1, title='Retained', unit='movie', seasons=[], type_badge='MOVIE',
               categories=['never_watched'], reasons=['Old'], reason='Old', size_bytes=10,
               disk=None, rating=None, rating_source=None)
    body = json.dumps({'sync': {'status': 'finished'},
                       'detection': {'status': 'finished',
                                     'counts': {'candidates': 1, 'candidate_rows': [row]}}})
    conn.execute("INSERT INTO scan_runs (status, mode, trigger, report)"
                 " VALUES ('finished', 'dry_run', 'manual', ?)", (body,))
    for number in range(31):
        conn.execute("INSERT INTO scan_runs (status, report) VALUES ('failed', ?)",
                     (json.dumps({'sync': {'status': 'failed'}, 'padding': 'x' * 100}),))
    conn.commit()
    # The previous build's own migration: the flat snapshot, the pruned reports.
    conn.executescript(f"""
        CREATE TABLE scan_run_views (
            run_id INTEGER PRIMARY KEY REFERENCES scan_runs(id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL,
            steps TEXT NOT NULL
        );
        INSERT INTO scan_run_views {LEGACY_V10_PROJECTION}
            WHERE id IN (SELECT id FROM scan_runs ORDER BY id DESC LIMIT 30)
               OR id = (SELECT MAX(id) FROM scan_runs WHERE status = 'finished');
        UPDATE scan_runs SET report = NULL WHERE id NOT IN
            (SELECT id FROM scan_runs ORDER BY id DESC LIMIT 30);
        CREATE TRIGGER scan_run_view_insert AFTER INSERT ON scan_runs BEGIN
            INSERT OR REPLACE INTO scan_run_views {LEGACY_V10_PROJECTION} WHERE id = NEW.id;
        END;
        CREATE TRIGGER scan_run_view_update AFTER UPDATE OF report ON scan_runs
            WHEN NEW.report IS NOT NULL BEGIN
            INSERT OR REPLACE INTO scan_run_views {LEGACY_V10_PROJECTION} WHERE id = NEW.id;
        END;
    """)
    conn.execute('PRAGMA user_version = 10')
    conn.commit()
    conn.close()
    # The upgrade to the current schema keeps the display of the old run.
    conn = init_db(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert report_section_rows(conn, 'candidate_rows') == [row]
    assert [item.title for item in candidates_view(conn).items] == ['Retained']
    # A section the old report never carried reads as an empty list.
    assert report_section_rows(conn, 'protected_rows') == []
    assert report_section_rows(conn, 'leaving_rows') == []
    conn.close()


def test_v11_failed_migration_restores_the_projection_and_its_triggers(tmp_path, monkeypatch):
    import json
    conn = connect(tmp_path / 'legacy.db')
    for script in MIGRATIONS[:10]:
        conn.executescript(script)
    conn.execute('PRAGMA user_version = 10')
    body = json.dumps({'detection': {'status': 'finished', 'counts': {'candidates': 1}}})
    conn.execute("INSERT INTO scan_runs (status, report) VALUES ('finished', ?)", (body,))
    conn.commit()
    report = conn.execute('SELECT report FROM scan_runs WHERE id=1').fetchone()[0]
    snapshot = conn.execute('SELECT snapshot FROM scan_run_views WHERE run_id=1').fetchone()[0]
    monkeypatch.setattr(db_module, 'MIGRATIONS', MIGRATIONS[:10] + (MIGRATIONS[10] + 'INVALID SQL;',))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 10
    assert conn.execute('SELECT report FROM scan_runs WHERE id=1').fetchone()[0] == report
    assert conn.execute('SELECT snapshot FROM scan_run_views WHERE run_id=1').fetchone()[0] == snapshot
    # The previous triggers are back: a new run is projected as before.
    conn.execute("INSERT INTO scan_runs (status, report) VALUES ('failed', '{}')")
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM scan_run_views').fetchone()[0] == 2
    monkeypatch.setattr(db_module, 'MIGRATIONS', MIGRATIONS)
    assert migrate(conn) == SCHEMA_VERSION
    assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_v9_migration_preserves_rows_and_defaults_presence_to_unknown_not_gone(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:8]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 8")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Film', 'movie')")
    conn.execute("INSERT INTO episodes (plex_rating_key, series_rating_key) VALUES ('e1', '1')")
    conn.commit()
    assert migrate(conn) == SCHEMA_VERSION
    assert tuple(conn.execute("SELECT title, plex_present FROM media_items").fetchone()) == ('Film', 1)
    assert tuple(conn.execute("SELECT plex_rating_key, plex_present FROM episodes").fetchone()) == ('e1', 1)
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v9_presence_columns_and_version_roll_back_together(tmp_path, monkeypatch):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:8]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS[:8] + (MIGRATIONS[8] + "INVALID SQL;",))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    for table in ('media_items', 'episodes'):
        assert 'plex_present' not in {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
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

    assert migrate(conn) == SCHEMA_VERSION

    indexes = {
        row["name"]
        for row in conn.execute("PRAGMA index_list('scan_runs')")
    }
    assert "idx_scan_runs_schedule_slot" in indexes
    run = conn.execute("SELECT status, trigger FROM scan_runs").fetchone()
    assert (run["status"], run["trigger"]) == ("finished", "schedule")
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v7_migration_adds_the_protections_item_index_without_changing_protections(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:6]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Film', 'movie')")
    conn.execute(
        "INSERT INTO protections (kind, media_item_id, detail) VALUES ('keep_forever', 1, 'movie')"
    )
    conn.commit()

    assert migrate(conn) == SCHEMA_VERSION

    indexes = {
        row["name"]
        for row in conn.execute("PRAGMA index_list('protections')")
    }
    assert "idx_protections_item" in indexes
    protection = conn.execute(
        "SELECT kind, media_item_id, detail FROM protections"
    ).fetchone()
    assert (protection["kind"], protection["media_item_id"], protection["detail"]) == (
        "keep_forever",
        1,
        "movie",
    )
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_v8_migration_adds_the_history_instant_index_without_changing_events(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    for script in MIGRATIONS[:7]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 7")
    conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, occurred_at)"
        " VALUES ('deleted', 'Film', 'movie', '2026-11-01T12:00:00+00:00')"
    )
    conn.commit()

    assert migrate(conn) == SCHEMA_VERSION

    indexes = {
        row["name"]
        for row in conn.execute("PRAGMA index_list('removal_events')")
    }
    assert "idx_removal_events_instant" in indexes
    event = conn.execute(
        "SELECT outcome, title, occurred_at FROM removal_events"
    ).fetchone()
    assert (event["outcome"], event["title"], event["occurred_at"]) == (
        "deleted",
        "Film",
        "2026-11-01T12:00:00+00:00",
    )
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_a_migration_that_fails_halfway_leaves_the_previous_schema_and_version_intact(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    conn = connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    # a real multi-statement migration whose first statement succeeds and a
    # later one fails, as an interrupted migration 003 would produce
    broken_migrations = MIGRATIONS[:2] + (
        "ALTER TABLE removal_requests ADD COLUMN last_error TEXT;"
        " ALTER TABLE removal_requests ADD COLUMN last_error TEXT;"
        " CREATE TABLE libraries (id INTEGER);",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", broken_migrations)
        failing = connect(path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                migrate(failing)
            assert failing.execute("PRAGMA user_version").fetchone()[0] == 2
            columns = {
                row[1]
                for row in failing.execute("PRAGMA table_info('removal_requests')")
            }
            assert "last_error" not in columns
        finally:
            failing.close()

    # the database is still openable and migrates cleanly with the real chain
    conn = init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_a_schema_newer_than_the_code_is_refused_without_being_modified(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    init_db(path).close()
    newer_version = SCHEMA_VERSION + 1
    conn = connect(path)
    conn.execute(f"PRAGMA user_version = {newer_version}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError) as excinfo:
        init_db(path)
    assert str(newer_version) in str(excinfo.value)
    assert str(SCHEMA_VERSION) in str(excinfo.value)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == newer_version
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
    finally:
        conn.close()


def test_two_backups_in_the_same_second_do_not_collide(tmp_path):
    """S4 (#40): a name collision must not stop the migration."""
    backups = tmp_path / "backups"
    backups.mkdir()
    first = db_module._create_backup_file(backups, "almali-library-curator", "20260103T000000Z")
    second = db_module._create_backup_file(backups, "almali-library-curator", "20260103T000000Z")
    assert first != second
    assert second.exists()
    assert second.stat().st_mode & 0o777 == 0o600


def test_a_pending_migration_copies_the_database_to_backups_first(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    conn = connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute(
        "INSERT INTO media_items (plex_rating_key, title, item_type) VALUES ('1', 'Film', 'movie')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    init_db(path).close()

    backups = sorted((tmp_path / "backups").glob("almali-library-curator-*.db"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    backup = connect(backups[0])
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("SELECT title FROM media_items").fetchone()[0] == "Film"
    finally:
        backup.close()


def test_backups_keep_only_the_five_most_recent_copies(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    conn = connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for index in range(6):
        stale = backup_dir / f"almali-library-curator-2026010{index + 1}T000000Z.db"
        stale.write_bytes(b"stale")

    init_db(path).close()

    backups = sorted(backup_dir.glob("almali-library-curator-*.db"))
    assert len(backups) == 5
    assert backups[0].name == "almali-library-curator-20260103T000000Z.db"
    assert backups[-1].name != "almali-library-curator-20260101T000000Z.db"


def test_a_new_or_in_memory_database_is_not_backed_up(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    init_db(path).close()
    assert not (tmp_path / "backups").exists()

    init_db(":memory:").close()


def test_an_up_to_date_database_is_not_copied_on_startup(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    init_db(path).close()  # a new database migrates without any backup

    init_db(path).close()  # nothing pending: no copy, no backups directory

    assert not (tmp_path / "backups").exists()


def test_a_failed_backup_prevents_the_migration(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    conn = connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    # a regular file where the backups directory must land makes the copy
    # impossible, so the migration must not run
    (tmp_path / "backups").write_bytes(b"not a directory")

    with pytest.raises(Exception):
        init_db(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_a_newer_schema_is_refused_without_changing_the_journal_mode(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    init_db(path).close()
    newer_version = SCHEMA_VERSION + 1
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute(f"PRAGMA user_version = {newer_version}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        init_db(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == newer_version
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path):
    conn = init_db(tmp_path / "almali-library-curator.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_on_a_current_database_leaves_the_caller_transaction_open(tmp_path):
    conn = init_db(tmp_path / "almali-library-curator.db")
    conn.execute("INSERT INTO app_meta (key, value) VALUES ('probe', 'pending')")
    assert conn.in_transaction

    assert migrate(conn) == SCHEMA_VERSION

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM app_meta WHERE key = 'probe'").fetchone()[0] == 0
    conn.close()


def test_migrate_refuses_a_pending_migration_inside_a_caller_transaction(tmp_path):
    path = tmp_path / "almali-library-curator.db"
    conn = connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                 " VALUES ('1', 'Movies', 'movie')")
    assert conn.in_transaction

    with pytest.raises(sqlite3.ProgrammingError, match="open transaction"):
        migrate(conn)

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
