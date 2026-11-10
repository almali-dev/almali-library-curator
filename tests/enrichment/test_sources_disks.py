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


def test_sync_invalidates_old_scope_even_when_the_following_file_refresh_fails(conn, settings):
    from almali_library_curator.scan.pipeline import default_steps
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
    # Sync changed the library but not the disk. The old scope must no
    # longer be served, even though file refresh could not replace it.
    assert sources_view(conn, replace(settings, curated_libraries=('1',))) == SourcesView(2, None, 0)
    assert sources_view(conn, replace(settings, curated_libraries=('3',))) == SourcesView(2, None, 1)
    assert sources_view(conn, settings) == SourcesView(2, None, 2)


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
