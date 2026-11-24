"""Disk summaries are durable, scoped like the panel, and atomic with file facts."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from almali_library_curator.enrichment import refresh_files
from almali_library_curator.web.models import SourcesView
from almali_library_curator.web.views import sources_view


def seed(conn):
    conn.executemany(
        'INSERT INTO libraries (plex_key, title, section_type) VALUES (?, ?, ?)',
        [('1', 'Movies', 'movie'), ('2', 'Shows', 'show'), ('3', 'Vault', 'movie')])
    conn.executemany(
        'INSERT INTO media_items (library_id, plex_rating_key, title, item_type, file_path, disk)'
        ' VALUES ((SELECT id FROM libraries WHERE plex_key=?), ?, ?, ?, ?, ?)',
        [('1', '100', 'Movie', 'movie', 'movie', 'old'),
         ('2', '200', 'Show', 'series', None, 'series-disk'),
         ('3', '300', 'Vault', 'movie', 'vault', 'vault-disk')])
    conn.executemany(
        'INSERT INTO episodes (plex_rating_key, series_rating_key, media_item_id, file_path, disk)'
        ' VALUES (?, ?, (SELECT id FROM media_items WHERE plex_rating_key=?), ?, ?)',
        [('201', '200', '200', 'episode', 'old'),
         ('202', '200', None, 'unlinked', 'unlinked-disk'),
         ('999', 'unknown', None, 'orphan', 'orphan-disk')])
    conn.commit()


def host(disk='new'):
    return SimpleNamespace(file_facts=lambda path: SimpleNamespace(
        size_bytes=42, hardlink_count=1, disk=disk))


def assert_cached(conn, settings, count):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        # #122: the fake server's accounts never played, so none is counted.
        assert sources_view(conn, settings) == SourcesView(0, None, count)
    finally:
        conn.set_trace_callback(None)
    assert not any('union' in sql.lower() for sql in statements)
    assert not any('media_items' in sql.lower() or 'episodes' in sql.lower() for sql in statements)


def test_scoped_refresh_summarizes_stored_facts_including_deselected_and_unlinked_rows(conn, settings):
    seed(conn)
    selected = replace(settings, curated_libraries=('1', '2'))
    summary = refresh_files(conn, host(), selected)
    assert (summary.movies, summary.episodes, summary.unavailable) == (1, 1, 0)
    assert tuple(conn.execute("SELECT file_size_bytes, hardlink_count, disk FROM media_items"
                              " WHERE plex_rating_key='100'").fetchone()) == (42, 1, 'new')
    assert conn.execute("SELECT disk FROM media_items WHERE plex_rating_key='300'").fetchone()[0] == 'vault-disk'
    # Scope may change after the scan. Global facts include a true orphan;
    # scoped facts include an episode matched only by series_rating_key.
    assert_cached(conn, settings, 5)
    assert_cached(conn, selected, 3)
    assert_cached(conn, replace(settings, curated_libraries=('2',)), 3)
    assert_cached(conn, replace(settings, curated_libraries=('3',)), 1)
    assert_cached(conn, replace(settings, curated_libraries=('missing',)), 0)


@pytest.mark.parametrize('empty', [True, False])
def test_an_empty_disk_set_is_cached_rather_than_treated_as_missing(conn, settings, empty):
    if not empty:
        seed(conn)
        # Series facts are not refreshed; clear this one separately.
        conn.execute('UPDATE media_items SET disk=NULL')
        conn.commit()
    summary = refresh_files(conn, None, settings)
    assert (summary.movies, summary.episodes, summary.unavailable) == ((0, 0, 0) if empty else (2, 3, 5))
    assert_cached(conn, settings, 0)
    assert_cached(conn, replace(settings, curated_libraries=('2',)), 0)


def test_disk_cache_write_failure_rolls_back_file_facts_and_keeps_previous_panel(conn, settings):
    seed(conn)
    refresh_files(conn, host(), settings)
    assert_cached(conn, settings, 2)
    conn.execute("CREATE TRIGGER reject_disk_summary BEFORE UPDATE ON app_meta"
                 " WHEN NEW.key='sources_disks_v1'"
                 " BEGIN SELECT RAISE(ABORT, 'disk summary rejected'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='disk summary rejected'):
        refresh_files(conn, host('replacement'), settings)
    assert {row[0] for row in conn.execute('SELECT disk FROM episodes')} == {'new'}
    assert conn.execute("SELECT disk FROM media_items WHERE plex_rating_key='100'").fetchone()[0] == 'new'
    assert_cached(conn, settings, 2)


def test_a_sync_records_the_scope_it_changed_even_when_the_file_refresh_fails(conn, settings):
    """#36: the recorded sets are written where the move is committed.

    The old scope must never be served again, and serving the new one must
    not cost a UNION on every render: the record and the database commit
    together, so a failed refresh cannot leave the panel behind.
    """
    from almali_library_curator.enrichment.sources import cached_disks
    from almali_library_curator.scan.pipeline import default_steps
    from almali_library_curator.web.views import _source_disks
    from fake_plex_server import FakePlexDataset, FakePlexServer, movie

    seed(conn)
    refresh_files(conn, host(), settings)
    assert_cached(conn, replace(settings, curated_libraries=('1',)), 1)
    conn.execute('CREATE TRIGGER reject_file_refresh BEFORE UPDATE OF hardlink_count ON media_items'
                 " BEGIN SELECT RAISE(ABORT, 'refresh rejected'); END")
    conn.commit()
    data = FakePlexDataset()
    data.sections.append({'key': '3', 'title': 'Vault', 'type': 'movie'})
    data.items['3'] = [movie('100', 'Movie moved to Vault')]
    with FakePlexServer(data) as server:
        current = replace(settings, dry_run=False, plex_base_url=server.base_url, plex_token='test-token')
        steps = default_steps(conn, current, lambda: datetime.now(timezone.utc), None)
        steps['sync']()
        result = steps['enrichment']()
    assert result['errors'] == {'refresh_files': 'IntegrityError'}
    # Sync changed the library but not the disk: the recorded sets follow the
    # move, so the old scope is gone and the panel needs no per-render scan.
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert sources_view(conn, replace(settings, curated_libraries=('1',))) == SourcesView(0, None, 0)
        assert sources_view(conn, replace(settings, curated_libraries=('3',))) == SourcesView(0, None, 1)
        assert sources_view(conn, settings) == SourcesView(0, None, 2)
    finally:
        conn.set_trace_callback(None)
    assert not any('union' in sql.lower() for sql in statements)
    assert not any('media_items' in sql.lower() or 'episodes' in sql.lower() for sql in statements)
    # Never wrong: the record answers exactly what the live query would.
    for keys in ((), ('1',), ('2',), ('3',), ('1', '2', '3'), ('missing',)):
        assert cached_disks(conn, keys) == _source_disks(conn, keys), keys


def test_a_failed_sync_leaves_the_record_and_the_panel_on_it(conn, settings):
    """A sync that rolls back changed nothing, so the record still applies."""
    from almali_library_curator.plex import PlexUnavailableError
    from almali_library_curator.scan.pipeline import default_steps
    from fake_plex_server import FakePlexDataset, FakePlexServer

    seed(conn)
    refresh_files(conn, host(), settings)
    before = conn.execute(
        "SELECT value FROM app_meta WHERE key='sources_disks_v1'").fetchone()[0]
    data = FakePlexDataset()
    data.always_fail = {'/library/sections': 500}
    with FakePlexServer(data) as server:
        current = replace(settings, dry_run=False, plex_base_url=server.base_url,
                          plex_token='test-token')
        steps = default_steps(conn, current, lambda: datetime.now(timezone.utc), None)
        with pytest.raises(PlexUnavailableError):
            steps['sync']()
    assert conn.execute(
        "SELECT value FROM app_meta WHERE key='sources_disks_v1'").fetchone()[0] == before
    assert_cached(conn, settings, 2)


def test_a_disk_record_that_cannot_be_written_rolls_the_sync_back(conn, settings):
    """The record and the snapshot it describes commit together, or not at all.

    A record that could not be written next to its own snapshot would let the
    panel keep serving the scope from before the move.
    """
    from almali_library_curator.scan.pipeline import default_steps
    from fake_plex_server import FakePlexDataset, FakePlexServer, movie

    seed(conn)
    refresh_files(conn, host(), settings)
    recorded = conn.execute(
        "SELECT value FROM app_meta WHERE key='sources_disks_v1'").fetchone()[0]
    conn.execute("CREATE TRIGGER reject_disk_summary BEFORE INSERT ON app_meta"
                 " WHEN NEW.key='sources_disks_v1'"
                 " BEGIN SELECT RAISE(ABORT, 'disk summary rejected'); END")
    conn.commit()
    data = FakePlexDataset()
    data.sections.append({'key': '3', 'title': 'Vault', 'type': 'movie'})
    data.items['1'] = [movie('100', 'Movie')]
    data.items['3'] = [movie('300', 'Vault')]
    with FakePlexServer(data) as server:
        current = replace(settings, dry_run=False, plex_base_url=server.base_url,
                          plex_token='test-token')
        steps = default_steps(conn, current, lambda: datetime.now(timezone.utc), None)
        with pytest.raises(sqlite3.IntegrityError, match='disk summary rejected'):
            steps['sync']()
    # No part of the sync is published either: the users it read are gone, so
    # the panel's old record still describes the database and stays usable.
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT library_id FROM media_items"
                        " WHERE plex_rating_key='300'").fetchone()[0] == 3
    assert conn.execute("SELECT plex_present FROM media_items"
                        " WHERE plex_rating_key='300'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT value FROM app_meta WHERE key='sources_disks_v1'").fetchone()[0] == recorded
    assert_cached(conn, settings, 2)


@pytest.mark.parametrize('rounds', [1, 2])
def test_the_panel_stays_recorded_while_only_the_refresh_keeps_failing(conn, settings, rounds):
    """#36: the slow path must not come back for as long as the refresh fails."""
    from almali_library_curator.scan.pipeline import default_steps
    from almali_library_curator.web.views import _source_disks
    from fake_plex_server import FakePlexDataset, FakePlexServer, movie

    seed(conn)
    refresh_files(conn, host(), settings)
    conn.execute('CREATE TRIGGER reject_file_refresh BEFORE UPDATE OF hardlink_count ON media_items'
                 " BEGIN SELECT RAISE(ABORT, 'refresh rejected'); END")
    conn.commit()
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Movie')]
    with FakePlexServer(data) as server:
        current = replace(settings, dry_run=False, plex_base_url=server.base_url,
                          plex_token='test-token')
        for _ in range(rounds):
            steps = default_steps(conn, current, lambda: datetime.now(timezone.utc), None)
            steps['sync']()
            assert steps['enrichment']()['errors'] == {'refresh_files': 'IntegrityError'}
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                view = sources_view(conn, settings)
            finally:
                conn.set_trace_callback(None)
            assert view.disk_count == len(_source_disks(conn, ()))
            assert not any('union' in sql.lower() for sql in statements)
            assert not any(
                'media_items' in sql.lower() or 'episodes' in sql.lower()
                for sql in statements)


def test_real_sync_then_successful_refresh_rerecords_the_disks_the_panel_serves(conn, settings):
    from almali_library_curator.db import connect
    from almali_library_curator.enrichment import run_enrichment
    from almali_library_curator.enrichment.sources import cached_disks
    from almali_library_curator.scan.pipeline import default_steps
    from almali_library_curator.web.views import _source_disks
    from fake_plex_server import FakePlexDataset, FakePlexServer, movie

    def render_traced(reader, scope):
        statements = []
        reader.set_trace_callback(statements.append)
        try:
            return sources_view(reader, scope), statements
        finally:
            reader.set_trace_callback(None)

    seed(conn)
    refresh_files(conn, host('old-disk'), replace(settings, curated_libraries=('1', '2')))
    assert_cached(conn, replace(settings, curated_libraries=('1',)), 1)
    # A separate connection models another gunicorn worker: it must see the
    # re-recorded value without any process-local cache.
    worker = connect(conn.execute('PRAGMA database_list').fetchone()[2])
    try:
        data = FakePlexDataset()
        data.sections.append({'key': '3', 'title': 'Vault', 'type': 'movie'})
        data.items['3'] = [movie('100', 'Movie moved to Vault'), movie('300', 'Vault')]
        # This test exercises surviving file facts after a move. Return those
        # survivors explicitly: a complete empty section now proves absence.
        from fake_plex_server import series, season, episode
        data.items['2'] = [series('200', 'Show', childCount=1, leafCount=1)]
        data.children['200'] = [season('210', '200', 1, leafCount=1)]
        data.children['210'] = [episode('201', '200', '210', 1)]
        with FakePlexServer(data) as server:
            current = replace(settings, dry_run=False, curated_libraries=('2', '3'),
                              plex_base_url=server.base_url, plex_token='test-token')
            # The pipeline's own steps: the sync runs unmodified (it really
            # invalidates the record), and the enrichment step is the
            # production runner over this suite's fake host filesystem.
            steps = default_steps(
                conn, current, lambda: datetime.now(timezone.utc),
                lambda conn_, settings_, **services: run_enrichment(conn_, settings_, hostfs=host('fresh')))
            steps['sync']()
            # The sync records the scope it just changed, inside the
            # transaction that publishes it (#36): another worker answers
            # from the record with no UNION, and the moved movie's old scope
            # is already gone (its recorded count for library 1 was 1).
            recorded = render_traced(worker, settings)
            assert recorded[0] == SourcesView(0, None, 5)
            assert not any('union' in sql.lower() for sql in recorded[1])
            assert not any(
                'media_items' in sql.lower() or 'episodes' in sql.lower()
                for sql in recorded[1])
            assert render_traced(
                worker, replace(settings, curated_libraries=('1',)))[0] == SourcesView(0, None, 0)

            result = steps['enrichment']()

        assert result['errors'] == {}
        assert result['files'] == {'movies': 2, 'episodes': 1, 'unavailable': 0}
        # The recorded value is the fresh one: the per-library mapping and the
        # global set match what the live query gives after that sync.
        for keys in ((), ('1',), ('2',), ('3',), ('1', '2', '3'), ('missing',)):
            assert cached_disks(conn, keys) == _source_disks(conn, keys), keys
        assert cached_disks(conn, ('1',)) == set()
        assert cached_disks(conn, ('2',)) == {'fresh', 'series-disk', 'unlinked-disk'}
        assert cached_disks(conn, ('3',)) == {'fresh'}
        assert cached_disks(conn, ()) == {'fresh', 'series-disk', 'unlinked-disk', 'orphan-disk'}
        # The worker's panel then answers from the recorded value: no UNION
        # and no media_items/episodes query runs, and the disk count equals
        # the live query's count.
        renders = [render_traced(worker, scope) for scope in (
            settings,
            replace(settings, curated_libraries=('1',)),
            replace(settings, curated_libraries=('3',)))]
        assert [view for view, _ in renders] == [
            SourcesView(0, None, 4), SourcesView(0, None, 0), SourcesView(0, None, 1)]
        assert renders[0][0].disk_count == len(_source_disks(conn, ()))
        traced = [sql for _, statements in renders for sql in statements]
        assert not any('union' in sql.lower() for sql in traced)
        assert not any('media_items' in sql.lower() or 'episodes' in sql.lower() for sql in traced)
    finally:
        worker.close()


@pytest.mark.parametrize('value', ['invalid-json', 'null', '[]', '{}',
                                  '{"all": null, "libraries": {}}',
                                  '{"all": [1], "libraries": {}}',
                                  '{"all": ["new"], "libraries": {"2": null}}'])
def test_unreadable_cached_metadata_falls_back_to_original_panel_query(conn, settings, value):
    seed(conn)
    conn.execute('INSERT INTO app_meta (key, value) VALUES (?, ?)', ('sources_disks_v1', value))
    conn.commit()
    assert sources_view(conn, settings) == SourcesView(0, None, 5)
    assert sources_view(conn, replace(settings, curated_libraries=('2',))) == SourcesView(0, None, 3)


def test_a_concurrent_owner_write_is_not_blocked_while_the_sync_fetches(conn, settings):
    """#36 regression: the writer slot belongs to the publish, not the fetch.

    The disk sets are recorded with the snapshot they describe, inside the
    sync's own publish transaction. Holding the slot from before the Plex
    fetch would make every Mark or Keep answer "database is locked" after
    the busy timeout for the whole network phase (#87, #90).
    """
    import time

    from almali_library_curator.db import connect
    from almali_library_curator.scan.pipeline import default_steps
    from fake_plex_server import FakePlexDataset, FakePlexServer, history_entry, movie

    seed(conn)
    # A second connection to the same database models another gunicorn worker.
    live_path = conn.execute("PRAGMA database_list").fetchone()[2]
    observed = []

    def probe(request):
        # Only once, deep inside the fetch: the history is read after every
        # section, so the write must be able to commit at that moment.
        if observed or request["path"] != "/status/sessions/history/all":
            return
        writer = connect(live_path)
        writer.execute("PRAGMA busy_timeout = 200")
        started = time.monotonic()
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO app_meta (key, value) VALUES ('probe', 'web')")
            writer.execute("ROLLBACK")
            outcome = "wrote"
        except sqlite3.OperationalError as error:
            outcome = f"blocked: {error}"
        finally:
            writer.close()
        observed.append((outcome, time.monotonic() - started))

    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Movie')]
    data.history = [history_entry('100', 1, 1_700_000_000, 100, 100)]
    data.on_request = probe
    with FakePlexServer(data) as server:
        current = replace(settings, dry_run=False, plex_base_url=server.base_url,
                          plex_token='test-token')
        steps = default_steps(conn, current, lambda: datetime.now(timezone.utc), None)
        steps['sync']()

    assert observed, "the probe never ran during the fetch"
    outcome, waited = observed[0]
    assert outcome == "wrote", outcome
    assert waited < 0.2
    # #36 still holds: the sync recorded the sets with its own snapshot, so
    # the panel answers from the record instead of scanning the library.
    assert conn.execute(
        "SELECT value FROM app_meta WHERE key='sources_disks_v1'").fetchone()[0] != 'null'
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        sources_view(conn, settings)
    finally:
        conn.set_trace_callback(None)
    assert not any('union' in sql.lower() for sql in statements)
