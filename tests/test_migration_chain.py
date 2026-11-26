"""S18: populated v1 databases survive the complete migration chain."""

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

import pytest

from almali_library_curator import db
from almali_library_curator.config import Settings
from almali_library_curator.db import (
    MIGRATIONS, SCHEMA_VERSION, SchemaVersionError, connect, init_db,
)
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.web.views import candidates_view, history_view, scan_status_view


# The migrated databases are read with the app's own clock: a moment after
# the seeded due date, so the request stays open and readable.
CLOCK = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

SEEDS = {
    "libraries": "INSERT INTO libraries (plex_key, title, section_type) VALUES ('1', 'Movies', 'movie')",
    "media_items": (
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type)"
        " VALUES (1, '11', 'Tenet', 'movie'), (1, '12', 'A series', 'series')"
    ),
    "episodes": (
        "INSERT INTO episodes (media_item_id, plex_rating_key, series_rating_key, title)"
        " VALUES (2, '121', '12', 'Pilot')"
    ),
    "users": "INSERT INTO users (plex_user_id, title) VALUES ('1', 'Marc')",
    "play_events": (
        "INSERT INTO play_events (user_id, plex_rating_key, watched, played_at, position_ms)"
        " VALUES (1, '11', 1, '2026-01-01', 0)"
    ),
    "protections": (
        "INSERT INTO protections (kind, media_item_id, detail)"
        " VALUES ('keep_forever', 1, 'movie')"
    ),
    # The scope carrier is what the engine reads to rebuild a request.
    "candidates": (
        "INSERT INTO candidates (media_item_id, category, reason, facts)"
        ' VALUES (1, \'never_watched\', \'Old candidate\', \'{"unit": "movie"}\')'
    ),
    "removal_requests": (
        "INSERT INTO removal_requests"
        " (media_item_id, candidate_id, category, reason, grace_days, due_at)"
        " VALUES (1, 1, 'never_watched', 'R', 14, '2026-11-16')"
    ),
    "removal_events": (
        "INSERT INTO removal_events (removal_request_id, media_item_id, outcome, title, unit, occurred_at)"
        " VALUES (1, 1, 'deleted', 'Old', 'movie', '2026-11-01T12:00:00+00:00')"
    ),
    "scan_runs": "INSERT INTO scan_runs (status) VALUES ('finished')",
    "app_meta": "INSERT INTO app_meta (key, value) VALUES ('legacy', 'preserved')",
}


def _build_v1(path):
    with closing(connect(path)) as conn:
        for script in MIGRATIONS[:1]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 1")
        for statement in SEEDS.values():
            conn.execute(statement)
        conn.commit()
        return {
            table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            for table in SEEDS
        }


def _assert_seeded_rows(conn, expected):
    # Compare every original column, including IDs, FKs and timestamps, while
    # allowing migrations to append new columns with their own defaults.
    for table, rows in expected.items():
        columns = ", ".join(f'"{column}"' for column in rows[0])
        actual = conn.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid')
        assert [dict(row) for row in actual] == rows, table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failed_version", [2, 6], ids=["first-pending", "mid-chain"])
def test_failed_migration_preserves_data_and_last_committed_version(tmp_path, monkeypatch, failed_version):
    path = tmp_path / "curator.db"
    expected = _build_v1(path)
    broken = list(MIGRATIONS)
    broken[failed_version - 1] += "UPDATE libraries SET title = 'corrupted'; INVALID SQL;"
    monkeypatch.setattr(db, "MIGRATIONS", tuple(broken))

    with pytest.raises(sqlite3.OperationalError, match='near "INVALID"'):
        init_db(path)

    with closing(connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == failed_version - 1
        _assert_seeded_rows(conn, expected)
        # Both failing scripts create an index before the injected error.
        # Neither the DDL nor the data change may escape its transaction.
        failed_index = "idx_play_events_unique" if failed_version == 2 else "idx_scan_runs_schedule_slot"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (failed_index,)).fetchone() is None
        if failed_version == 6:
            assert conn.execute("SELECT tmdb_id, year FROM media_items WHERE id = 1").fetchone()[:] == (None, None)
            assert conn.execute("SELECT last_error, deleted_at FROM removal_requests").fetchone()[:] == (None, None)

    backups = list((tmp_path / "backups").glob("curator-*.db"))
    assert len(backups) == 1
    with closing(connect(backups[0])) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        _assert_seeded_rows(backup, expected)

    # A real startup can resume from the last committed step after repair.
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        _assert_seeded_rows(conn, expected)


def test_full_chain_preserves_rows_defaults_and_one_pre_migration_backup(tmp_path):
    path = tmp_path / "curator.db"
    expected = _build_v1(path)
    with closing(connect(path)) as conn:
        old_schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()

    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        _assert_seeded_rows(conn, expected)
        assert [tuple(row) for row in conn.execute(
            "SELECT tmdb_id, tvdb_id, imdb_id, year, plex_present FROM media_items ORDER BY id"
        )] == [(None, None, None, None, 1)] * 2
        assert conn.execute("SELECT disk, hardlink_count, plex_present FROM episodes").fetchone()[:] == (None, None, 1)
        assert conn.execute("SELECT last_error, deleted_at FROM removal_requests").fetchone()[:] == (None, None)
        assert conn.execute(
            "SELECT reason_parts, cancel_params FROM removal_requests"
        ).fetchone()[:] == (None, None)
        assert conn.execute("SELECT reason_parts FROM removal_events").fetchone()[:] == (None,)
        assert conn.execute("SELECT trigger, report FROM scan_runs").fetchone()[:] == (None, None)
        assert conn.execute("SELECT run_id, snapshot FROM scan_run_views").fetchone()[:] == (1, '{"counts":null}')
        # Migration 014 keeps the latest published snapshot as indexed rows;
        # a run without a report publishes no row and no category.
        assert conn.execute("SELECT COUNT(*) FROM scan_rows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_row_categories").fetchone()[0] == 0
        # The app's own read paths answer on the migrated database: the rows
        # restored above are what the Leaving soon screen, the History page and
        # the scan status read through their public accessors.
        engine = LifecycleEngine(
            conn, settings=Settings(config_dir=tmp_path), clock=lambda: CLOCK
        )
        [request] = engine.open_requests()
        assert (request.id, request.media_item_id, request.candidate_id) == (1, 1, 1)
        assert (request.category.value, request.unit.value, request.seasons) == (
            "never_watched", "movie", ())
        assert (request.grace_days, request.due_at.date().isoformat()) == (14, "2026-11-16")
        history = history_view(conn)
        assert [(event.title, event.outcome, event.unit) for event in history.events] == [
            ("Old", "deleted", "movie")]
        assert history.freed_per_month == {"2026-11": 0}
        assert scan_status_view(conn).state == "finished"
        # The seeded run carries no report, so the candidates page reads an
        # empty projection instead of raising on the missing snapshot body.
        assert candidates_view(conn).items == ()
        migrated_dump = tuple(conn.iterdump())

    backups = list((tmp_path / "backups").glob("curator-*.db"))
    assert len(backups) == 1
    backup_bytes = backups[0].read_bytes()
    with closing(connect(backups[0])) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        _assert_seeded_rows(backup, expected)
        assert backup.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == old_schema

    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert tuple(conn.iterdump()) == migrated_dump
    assert list((tmp_path / "backups").glob("curator-*.db")) == backups
    assert backups[0].read_bytes() == backup_bytes


def test_connect_refuses_newer_schema_without_touching_seeded_rows(tmp_path):
    path = tmp_path / "future.db"
    expected = _build_v1(path)
    with closing(connect(path)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()

    with pytest.raises(SchemaVersionError, match="newer than this build"):
        connect(path)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        _assert_seeded_rows(conn, expected)


def test_migration_014_backfills_the_display_cache_from_the_published_snapshot(tmp_path):
    """An upgraded database shows its latest scan at once (S1, #48).

    The cache is derived: the migration rebuilds it from the snapshot that is
    already published, so the Library is not empty after the upgrade and the
    upgrade itself writes no display row of its own.
    """
    import json

    from almali_library_curator.web.views import candidates_view, protected_view

    path = tmp_path / "curator.db"
    candidate = dict(media_item_id=1, title="Tenet", unit="movie", seasons=[], type_badge="MOVIE",
                     categories=["never_watched"], reasons=["Old"], reason="Old", size_bytes=10,
                     disk="disk1", rating=7.0, rating_source="IMDb",
                     reason_parts=[{"key": "detection.reason.never_watched.movie",
                                    "params": {"date": "2024-01-15", "users": 3}}])
    protected = dict(candidate, media_item_id=2, title="Severance", size_bytes=20,
                     protection_kind="rating", protection_reason="Rated 8.7")
    body = json.dumps({"sync": {"status": "finished"},
                       "detection": {"status": "finished",
                                     "counts": {"candidates": 1, "protected": 1,
                                                "candidate_rows": [candidate],
                                                "protected_rows": [protected]}}})
    with closing(connect(path)) as conn:
        for script in MIGRATIONS[:13]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 13")
        conn.execute("INSERT INTO scan_runs (status, mode, trigger, report)"
                     " VALUES ('finished', 'dry_run', 'manual', ?)", (body,))
        conn.commit()
        # The previous build published the snapshot but had no display cache.
        assert conn.execute("SELECT COUNT(*) FROM scan_run_views").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'scan_rows'").fetchone()[0] == 0

    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = [tuple(row) for row in conn.execute(
            "SELECT kind, title, size_bytes, categories, protection_kind FROM scan_rows"
            " ORDER BY kind")]
        assert rows == [
            ("candidate", "Tenet", 10, '["never_watched"]', None),
            ("protected", "Severance", 20, '["never_watched"]', "rating"),
        ]
        assert [tuple(row) for row in conn.execute(
            "SELECT kind, category FROM scan_row_categories ORDER BY kind")] == [
            ("candidate", "never_watched"), ("protected", "never_watched")]
        # The screens read the backfilled rows, not the snapshot, at once.
        assert [item.title for item in candidates_view(conn).items] == ["Tenet"]
        assert [item.title for item in protected_view(conn).items] == ["Severance"]
        assert json.loads(conn.execute(
            "SELECT reason_parts FROM scan_rows WHERE kind = 'candidate'").fetchone()[0]
        ) == candidate["reason_parts"]


def test_migration_016_adds_the_version_table_and_keeps_every_stored_row(tmp_path):
    """S2 (#132): an upgraded v15 database keeps its items and gains the
    versions, additively. Nothing stored is rewritten or removed, and the
    new table starts empty: the rows are facts Plex sends again on the next
    sync, never something the migration could invent.
    """
    path = tmp_path / "curator.db"
    with closing(connect(path)) as conn:
        for script in MIGRATIONS[:15]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 15")
        conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                     " VALUES ('1', 'Movies', 'movie')")
        conn.execute(
            "INSERT INTO media_items (library_id, plex_rating_key, title, item_type,"
            " tmdb_id, file_path, file_size_bytes) VALUES"
            " (1, '11', 'Tenet', 'movie', 577922, '/media/Movies/Tenet.mkv', 12345)")
        conn.commit()
        before = [tuple(row) for row in conn.execute(
            "SELECT title, tmdb_id, file_path, file_size_bytes FROM media_items")]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'media_versions'"
        ).fetchone()[0] == 0

    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [tuple(row) for row in conn.execute(
            "SELECT title, tmdb_id, file_path, file_size_bytes FROM media_items")] == before
        assert conn.execute("SELECT COUNT(*) FROM media_versions").fetchone()[0] == 0
        # Every fact Plex may omit is nullable: missing is unknown, never 0.
        columns = {row["name"]: row for row in conn.execute(
            "PRAGMA table_info(media_versions)")}
        assert set(columns) == {
            "media_item_id", "position", "plex_media_id", "video_profile",
            "resolution", "video_codec", "bitrate_kbps", "file_path", "size_bytes"}
        assert {name for name, row in columns.items() if row["notnull"]} == {
            "media_item_id", "position"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
