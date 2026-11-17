"""S8 / #15: a dry run is a private copy, and nothing of it can reach the store.

The preview used to be a full ``conn.backup()`` into memory. Whether that
stays a copy or becomes something cheaper later, these are the contracts the
mechanism has to keep: every stored row and the schema version are identical
before and after, a step that raises leaves the same store behind, a reader
and a writer on the live database see nothing of the preview while it runs,
and the only live writes a dry run may make are its own report and the
deliberately kept TMDB answers (#74).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import connect, init_db
from almali_library_curator.scan import ScanRunner

# The dry run's own product: the report of what would happen, and the cached
# external answers it may keep. Everything else is stored state.
REPORT_TABLES = ("scan_runs", "scan_run_views", "scan_rows", "scan_row_categories",
                 "tmdb_cache")


def stored_tables(conn) -> tuple[str, ...]:
    return tuple(
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")
        if row[0] not in REPORT_TABLES
    )


def dump(conn) -> dict:
    """Every stored row of every table, plus the schema version."""
    tables = {}
    for table in stored_tables(conn):
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        tables[table] = [
            tuple(row) for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid")
        ]
    return {"user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables}


def settings_for(tmp_path) -> Settings:
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    return settings


def seed(conn) -> None:
    """One row of every stored table, so a leak anywhere is visible."""
    conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                 " VALUES ('1', 'Shows', 'show')")
    conn.execute("INSERT INTO media_items (library_id, plex_rating_key, title, item_type,"
                 " disk, file_size_bytes) VALUES (1, '200', 'Show', 'series', 'disk1', 10)")
    conn.execute("INSERT INTO episodes (media_item_id, plex_rating_key, series_rating_key,"
                 " title, disk) VALUES (1, '201', '200', 'Pilot', 'disk1')")
    conn.execute("INSERT INTO users (plex_user_id, title) VALUES ('1', 'Marc')")
    conn.execute("INSERT INTO play_events (user_id, plex_rating_key, watched, played_at,"
                 " position_ms, duration_ms, source) VALUES (1, '201', 1,"
                 " '2024-01-01T00:00:00+00:00', 0, 100000, 'plex')")
    conn.execute("INSERT INTO candidates (media_item_id, category, reason, facts)"
                 " VALUES (1, 'never_watched', 'Never watched', '{}')")
    conn.execute("INSERT INTO protections (kind, media_item_id, detail)"
                 " VALUES ('keep_forever', 1, 'series')")
    conn.execute("INSERT INTO removal_requests (media_item_id, category, reason, grace_days,"
                 " due_at) VALUES (1, 'never_watched', 'Never watched', 14, '2026-11-16')")
    conn.execute("INSERT INTO removal_events (removal_request_id, media_item_id, outcome,"
                 " title, unit, occurred_at) VALUES (1, 1, 'deleted', 'Show', 'seasons',"
                 " '2026-11-01T00:00:00+00:00')")
    conn.execute("INSERT INTO app_meta (key, value) VALUES ('sources_disks_v1',"
                 " '{\"all\": [\"disk1\"], \"libraries\": {\"1\": [\"disk1\"]}}')")
    conn.execute("INSERT INTO scan_runs (status, mode, trigger) VALUES ('finished',"
                 " 'live', 'manual')")
    conn.commit()


def writing_factory(observed: list | None = None, *, raise_in: str | None = None):
    """Steps that write over the whole store, like the production ones do."""

    def factory(work, loaded, clock, enrichment):
        def sync():
            work.execute("INSERT INTO libraries (plex_key, title, section_type)"
                         " VALUES ('2', 'Movies', 'movie')")
            work.execute(
                "INSERT INTO media_items (library_id, plex_rating_key, title, item_type)"
                " VALUES ((SELECT id FROM libraries WHERE plex_key='2'), '300', 'Film',"
                " 'movie')")
            work.execute("INSERT INTO users (plex_user_id, title) VALUES ('2', 'Anna')")
            work.execute("UPDATE play_events SET watched = 0")
            work.execute("DELETE FROM play_events")
            work.execute("UPDATE media_items SET disk = 'preview-disk'"
                         " WHERE plex_rating_key = '200'")
            work.execute("INSERT INTO app_meta (key, value) VALUES ('preview', 'yes')"
                         " ON CONFLICT(key) DO UPDATE SET value = 'yes'")
            work.commit()
            if observed is not None:
                observed.append(("sync", work.execute(
                    "SELECT COUNT(*) FROM play_events").fetchone()[0]))
            return {"media_items": 1}

        def enrich():
            if raise_in == "enrichment":
                raise RuntimeError("step failed")
            work.execute(
                "INSERT INTO tmdb_cache (item_type, tmdb_id, payload, fetched_at)"
                " VALUES ('series', '84', '{\"id\": 84}', '2026-11-01T00:00:00+00:00')")
            # The preview's own writes must be visible to the paying steps.
            assert work.execute("SELECT title FROM media_items"
                               " WHERE plex_rating_key = '300'").fetchone()[0] == "Film"
            work.commit()
            return {"cached": 1}

        def detection():
            if raise_in == "detection":
                raise RuntimeError("step failed")
            return {"candidates": 0, "candidate_rows": [], "protected": 0}

        return {"sync": sync, "enrichment": enrich, "detection": detection,
                "recheck": lambda: {}, "deletion": lambda: {}}

    return factory


def test_a_dry_run_leaves_every_stored_row_and_the_schema_as_it_was(tmp_path):
    settings = settings_for(tmp_path)
    conn = init_db(settings.database_path)
    try:
        seed(conn)
        before = dump(conn)
        report = ScanRunner(settings, steps_factory=writing_factory()).run("manual")

        assert (report.status, report.mode) == ("finished", "dry_run")
        assert dump(conn) == before
        # The product of the run is recorded, and only the product: the report
        # the Library shows, and the TMDB answer #74 keeps on purpose.
        assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2
        assert conn.execute("SELECT status, mode FROM scan_runs ORDER BY id DESC").fetchone()[:] == (
            "finished", "dry_run")
        assert [tuple(row) for row in conn.execute(
            "SELECT item_type, tmdb_id FROM tmdb_cache")] == [("series", "84")]
    finally:
        conn.close()


def test_a_dry_run_that_raises_midway_leaves_the_store_untouched(tmp_path):
    settings = settings_for(tmp_path)
    conn = init_db(settings.database_path)
    try:
        seed(conn)
        before = dump(conn)
        report = ScanRunner(
            settings, steps_factory=writing_factory(raise_in="enrichment")).run("manual")

        assert report.status == "failed"
        assert report.steps["enrichment"] == {"status": "failed", "error": "RuntimeError"}
        assert dump(conn) == before
        # Not even the step's cache write is kept when the step raised.
        assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_reader_and_a_writer_see_nothing_of_the_preview_while_it_runs(tmp_path):
    """The live database stays readable and writable during a dry run.

    A preview that held a write transaction would block the web app's marks
    for the whole scan instead of the moment it publishes one.
    """
    settings = settings_for(tmp_path)
    conn = init_db(settings.database_path)
    seed(conn)
    before = dump(conn)
    seen: list[dict] = []

    def factory(work, loaded, clock, enrichment):
        def sync():
            work.execute("INSERT INTO media_items (library_id, plex_rating_key, title,"
                         " item_type) VALUES (1, '999', 'Preview only', 'movie')")
            work.execute("UPDATE app_meta SET value = 'preview' WHERE key = 'sources_disks_v1'")
            work.commit()
            probe = connect(settings.database_path)
            probe.execute("PRAGMA busy_timeout = 1000")
            try:
                probe.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                probe.execute("INSERT INTO app_meta (key, value) VALUES ('probe', 'web')")
                probe.execute("ROLLBACK")
                waited = time.monotonic() - started
                seen.append({
                    "preview_rows": probe.execute(
                        "SELECT COUNT(*) FROM media_items WHERE plex_rating_key = '999'"
                    ).fetchone()[0],
                    "stored_meta": probe.execute(
                        "SELECT value FROM app_meta WHERE key = 'sources_disks_v1'"
                    ).fetchone()[0],
                    "waited": waited,
                })
            finally:
                probe.close()
            return {}

        return {"sync": sync, "enrichment": lambda: {}, "detection": lambda: {},
                "recheck": lambda: {}, "deletion": lambda: {}}

    try:
        report = ScanRunner(settings, steps_factory=factory).run("manual")
        assert report.status == "finished"
        [observation] = seen
        assert observation["preview_rows"] == 0  # the preview row never landed
        assert observation["stored_meta"] != "preview"  # nor did its update
        assert observation["waited"] < 1.0  # the writer was not blocked
        assert dump(conn) == before
    finally:
        conn.close()


def test_a_live_run_keeps_the_rows_its_steps_wrote(tmp_path):
    """The isolation belongs to the dry run, not to the report."""
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False, dry_run=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    try:
        seed(conn)
        report = ScanRunner(settings, steps_factory=writing_factory()).run("manual")
        assert (report.status, report.mode) == ("finished", "live")
        assert conn.execute(
            "SELECT title FROM media_items WHERE plex_rating_key = '300'").fetchone()[0] == "Film"
        assert conn.execute(
            "SELECT COUNT(*) FROM play_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_dry_run_enforces_the_same_constraints_as_the_live_database(tmp_path):
    """A plan a dry run reports must be one a live run could execute.

    The private copy came up without foreign keys, so a write the real
    database refuses succeeded in the preview and the run was reported as
    finished.
    """
    settings = settings_for(tmp_path)
    conn = init_db(settings.database_path)
    seed(conn)
    before = dump(conn)

    def factory(work, loaded, clock, enrichment):
        def sync():
            work.execute(
                "INSERT INTO play_events (user_id, plex_rating_key, watched, played_at,"
                " position_ms, source) VALUES (999, '201', 1,"
                " '2024-02-02T00:00:00+00:00', 0, 'tautulli')")
            work.commit()
            return {}

        return {"sync": sync, "enrichment": lambda: {}, "detection": lambda: {},
                "recheck": lambda: {}, "deletion": lambda: {}}

    try:
        report = ScanRunner(settings, steps_factory=factory).run("manual")
        assert report.status == "failed"
        assert report.steps["sync"] == {"status": "failed", "error": "IntegrityError"}
        assert dump(conn) == before
    finally:
        conn.close()


@pytest.mark.parametrize("trigger", ["manual"])
def test_the_preview_is_gone_when_the_run_returns(tmp_path, trigger):
    """No preview connection can outlive the run, or a later step would see it."""
    settings = settings_for(tmp_path)
    conn = init_db(settings.database_path)
    seed(conn)
    connections = []

    def factory(work, loaded, clock, enrichment):
        connections.append(work)
        return {name: (lambda: {}) for name in
                ("sync", "enrichment", "detection", "recheck", "deletion")}

    try:
        ScanRunner(settings, steps_factory=factory).run(trigger)
        [preview] = connections
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            preview.execute("SELECT 1")
    finally:
        conn.close()
