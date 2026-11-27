"""S2 (#148): the shared-use check and the scope probe read an index.

Migration 019 adds partial indexes only: they can be dropped and rebuilt, and
no query changes its result. The tests prove the plan the real queries run
(through ``EXPLAIN QUERY PLAN`` over the statements the code emitted) and
that an upgraded v18 database keeps every row it had.
"""

from contextlib import closing
import sqlite3

import pytest

from almali_library_curator.db import MIGRATIONS, SCHEMA_VERSION, connect, init_db

#: The indexes migration 019 adds, and the only thing it changes.
NEW_INDEXES = {
    "idx_media_items_file_path",
    "idx_episodes_file_path",
    "idx_media_items_radarr_id",
    "idx_media_items_sonarr_id",
    "idx_media_items_absent",
}

MOVIE_A = "/media/movies/A.mkv"
MOVIE_B = "/media/movies/B.mkv"
EPISODE_A = "/media/tv/Show/S01E01.mkv"


def _seed(conn):
    conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                 " VALUES ('1', 'Movies', 'movie'), ('2', 'Filmoteca', 'movie')")
    conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type,"
        " file_path, file_size_bytes, radarr_id, sonarr_id, plex_present) VALUES"
        " (1, '11', 'A', 'movie', ?, 10, 501, NULL, 1),"
        " (2, '12', 'A again', 'movie', ?, 10, 501, NULL, 1),"
        " (1, '21', 'Show', 'series', '/media/tv/Show', 20, NULL, 77, 1)",
        (MOVIE_A, MOVIE_A))
    conn.execute(
        "INSERT INTO episodes (media_item_id, plex_rating_key, series_rating_key,"
        " season_number, episode_number, file_path, file_size_bytes) VALUES"
        " (3, '211', '21', 1, 1, ?, 5), (3, '212', '21', 1, 2, ?, 5)",
        (EPISODE_A, EPISODE_A))
    # One proven-absent row is what makes the scope probe run at all.
    conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type, plex_present)"
        " VALUES (1, '31', 'Gone', 'movie', 0)")
    conn.commit()


def _indexes(conn):
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
    }


def _statements(conn, action):
    """Every statement ``action`` runs, in order."""
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, seen


def _plans(conn, statements):
    """The query plans of the statements a call emitted.

    ``EXPLAIN QUERY PLAN`` never runs the statement, so a traced statement is
    planned exactly as the code emitted it.
    """
    details = []
    for statement in statements:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            details.extend(row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN " + statement,
                [None] * statement.count("?")).fetchall())
    return details


def test_migration_019_only_adds_indexes_and_an_upgraded_v18_keeps_every_row(tmp_path):
    path = tmp_path / "curator.db"
    with closing(connect(path)) as conn:
        for script in MIGRATIONS[:18]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 18")
        _seed(conn)
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("libraries", "media_items", "episodes")
        }
        schema_before = conn.execute(
            "SELECT name FROM sqlite_master ORDER BY name").fetchall()
        assert not (_indexes(conn) & NEW_INDEXES)

    with closing(init_db(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 19
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid")] == rows, table
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # Only indexes are new: no table, column or trigger of the schema moved.
        added = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master ORDER BY name")} - {row[0] for row in schema_before}
        assert added == NEW_INDEXES
        assert _indexes(conn) >= NEW_INDEXES
        # A full rebuild keeps the plan the indexes exist for.
        conn.execute("REINDEX")
        assert _indexes(conn) >= NEW_INDEXES


def test_shared_use_check_reads_an_index_instead_of_scanning(tmp_path):
    """S2: every lookup of the sharing check is an index search (#136)."""
    from almali_library_curator.detection.models import Unit
    from almali_library_curator.lifecycle.deletion import shared_use_conflict

    conn = init_db(tmp_path / "curator.db")
    try:
        _seed(conn)
        seen = _statements(conn, lambda: shared_use_conflict(
            conn, 1, Unit.MOVIE, [MOVIE_A]))
        # The check still refuses: the second copy carries the arr entry the
        # removal would take with it (an arr refusal outranks the file one).
        conflict = seen[0]
        assert conflict is not None
        assert list(conflict.libraries) == ["Filmoteca"]
        details = _plans(conn, seen[1])
        assert details
        assert not [detail for detail in details if detail.startswith("SCAN")], details
    finally:
        conn.close()


def test_shared_use_episode_path_lookup_reads_an_index(tmp_path):
    from almali_library_curator.detection.models import Unit
    from almali_library_curator.lifecycle.deletion import shared_use_conflict

    conn = init_db(tmp_path / "curator.db")
    try:
        _seed(conn)
        # A second series holding the same episode file is another user.
        conn.execute(
            "INSERT INTO media_items (library_id, plex_rating_key, title, item_type)"
            " VALUES (2, '22', 'Other show', 'series')")
        conn.execute(
            "INSERT INTO episodes (media_item_id, plex_rating_key, series_rating_key,"
            " season_number, episode_number, file_path) VALUES (4, '221', '22', 1, 1, ?)",
            (EPISODE_A,))
        conn.commit()
        _, seen = _statements(conn, lambda: shared_use_conflict(
            conn, 3, Unit.SERIES, [EPISODE_A]))
        details = _plans(conn, seen)
        assert details
        assert not [detail for detail in details if detail.startswith("SCAN")], details
    finally:
        conn.close()


def test_the_curated_scope_probe_reads_an_index(tmp_path):
    """S2/S3: the ``plex_present = 0`` probe of the page's scope is indexed."""
    from almali_library_curator.scan import rows as scan_rows

    conn = init_db(tmp_path / "curator.db")
    try:
        _seed(conn)
        seen = _statements(conn, lambda: scan_rows._scope(conn, settings=None))
        # No settings means no restriction, and nothing is read at all.
        assert not seen[1]
        # With settings the probe answers whether any row was proven absent.
        from dataclasses import replace

        from almali_library_curator.config import Settings

        settings = replace(Settings(config_dir=tmp_path), database_path=":memory:")
        (predicate, params), seen = _statements(
            conn, lambda: scan_rows._scope(conn, settings))
        assert params == ()
        details = _plans(conn, seen)
        assert [detail for detail in details if "media_items" in detail]
        assert not [detail for detail in details if detail.startswith("SCAN")], details
        # The predicate itself is unchanged: the scope still admits present rows.
        assert predicate.startswith("media_item_id IN")
        assert conn.execute(
            f"SELECT COUNT(*) FROM scan_rows WHERE {predicate}", params
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_every_new_index_is_a_partial_or_single_column_index_on_a_hot_column(tmp_path):
    conn = init_db(tmp_path / "curator.db")
    try:
        indexed = {
            row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index'")
            if row[0] in NEW_INDEXES
        }
        assert set(indexed) == NEW_INDEXES
        assert "CREATE INDEX idx_episodes_file_path ON episodes(file_path)" == indexed[
            "idx_episodes_file_path"][1]
        assert "CREATE INDEX idx_media_items_file_path ON media_items(file_path)" == indexed[
            "idx_media_items_file_path"][1]
        assert "radarr_id IS NOT NULL" in indexed["idx_media_items_radarr_id"][1]
        assert "sonarr_id IS NOT NULL" in indexed["idx_media_items_sonarr_id"][1]
        assert "plex_present = 0" in indexed["idx_media_items_absent"][1]
    finally:
        conn.close()


def test_a_partial_index_is_used_only_when_its_predicate_holds(tmp_path):
    """The partial indexes stay out of the way of the rows they exclude."""
    conn = init_db(tmp_path / "curator.db")
    try:
        _seed(conn)
        plans = [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT MIN(id) FROM media_items WHERE plex_present = 0")]
        assert plans == ["SEARCH media_items USING COVERING INDEX idx_media_items_absent"]
        plans = [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM media_items WHERE radarr_id = 501")]
        assert any("idx_media_items_radarr_id" in plan for plan in plans), plans
    finally:
        conn.close()


@pytest.mark.parametrize("table,column", [("episodes", "file_path"),
                                          ("media_items", "file_path")])
def test_the_file_path_lookup_serves_the_sharing_check(tmp_path, table, column):
    conn = init_db(tmp_path / "curator.db")
    try:
        _seed(conn)
        plans = [row[3] for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT {column} FROM {table}"
            f" WHERE {column} IN ('{MOVIE_A}', '{MOVIE_B}')")]
        assert any("INDEX" in plan and "file_path" in plan for plan in plans), plans
        assert not any(plan.startswith("SCAN") for plan in plans), plans
    finally:
        conn.close()
