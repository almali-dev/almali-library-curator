"""S2: the public sources model preserves scope without per-render file scans."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from almali_library_curator.db import connect
from almali_library_curator.enrichment import run_enrichment
from almali_library_curator.scan import ScanRunner
from almali_library_curator.web.models import SourcesView
from almali_library_curator.web.views import sources_view


def seed_sources(conn):
    conn.executemany(
        'INSERT INTO libraries (plex_key, title, section_type) VALUES (?, ?, ?)',
        [('1', 'Movies', 'movie'), ('2', 'Shows', 'show'), ('3', 'Vault', 'movie')])
    conn.executemany(
        'INSERT INTO media_items (library_id, plex_rating_key, title, item_type, file_path, disk)'
        ' VALUES ((SELECT id FROM libraries WHERE plex_key=?), ?, ?, ?, ?, ?)',
        [('1', '100', 'Movie', 'movie', 'movie', 'disk1'),
         ('1', '101', 'Duplicate', 'movie', 'duplicate', 'disk1'),
         ('1', '102', 'Unavailable', 'movie', 'unavailable', None),
         ('1', '103', 'Empty disk', 'movie', 'empty', ''),
         ('2', '200', 'Show', 'series', None, 'disk2'),
         ('3', '300', 'Vault movie', 'movie', 'vault', 'disk3')])
    conn.executemany(
        'INSERT INTO episodes (plex_rating_key, series_rating_key, file_path, disk) VALUES (?, ?, ?, ?)',
        [('201', '200', 'episode', 'disk4'),
         ('202', '200', 'shared', 'disk1'),
         # Match the existing series-key join even without media_item_id.
         ('203', '200', 'unknown', None),
         ('999', 'missing', 'orphan', 'disk5')])
    conn.executemany('INSERT INTO users (plex_user_id, title) VALUES (?, ?)',
                     [('1', 'Owner'), ('2', 'Guest')])
    conn.executemany(
        'INSERT INTO play_events (plex_rating_key, played_at, source) VALUES (?, ?, ?)',
        [('100', '2020-01-01', 'plex'), ('100', '2022-01-01', 'tautulli'),
         ('300', '2021-01-01', 'tautulli')])
    conn.commit()


def host():
    disks = {'movie': 'disk1', 'duplicate': 'disk1', 'empty': '', 'vault': 'disk3',
             'episode': 'disk4', 'shared': 'disk1', 'orphan': 'disk5'}
    return SimpleNamespace(file_facts=lambda path: SimpleNamespace(
        size_bytes=10, hardlink_count=1, disk=disks.get(path)))


def scan(settings):
    def steps(conn, current, clock, enrichment_step):
        return {**{name: lambda: {} for name in ('sync', 'detection', 'recheck', 'deletion')},
                'enrichment': lambda: {'movies': run_enrichment(conn, current, hostfs=host()).files.movies}}
    return ScanRunner(settings, steps_factory=steps, settings_loader=lambda _: settings).run('manual')


@pytest.mark.parametrize('keys, count', [(None, 5), ((), 5), (('1',), 1), (('2',), 3),
                                         (('1', '2'), 3), (('3',), 1), (('missing',), 0)])
def test_sources_before_first_scan_preserve_all_fields_and_scope(app, conn, keys, count):
    seed_sources(conn)
    assert conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0] == 0
    settings = None if keys is None else replace(app.config['SETTINGS'], curated_libraries=keys)
    assert sources_view(conn, settings) == SourcesView(2, '2021-01-01', count)


@pytest.mark.parametrize('keys, count', [((), 5), (('1',), 1), (('2',), 3),
                                         (('1', '2'), 3), (('3',), 1), (('missing',), 0)])
def test_scan_records_cross_worker_disk_sets_without_render_union(app, conn, keys, count):
    seed_sources(conn)
    settings = replace(app.config['SETTINGS'], dry_run=False)
    before = sources_view(conn, replace(settings, curated_libraries=keys))
    assert scan(settings).status == 'finished'
    # A new connection models another gunicorn worker, not a process-local cache.
    other = connect(settings.database_path)
    statements = []
    try:
        other.set_trace_callback(statements.append)
        assert sources_view(other, replace(settings, curated_libraries=keys)) == before
        assert before == SourcesView(2, '2021-01-01', count)
        assert sources_view(other) == SourcesView(2, '2021-01-01', 5)
    finally:
        other.close()
    assert not any('union' in sql.lower() for sql in statements)
    assert not any('media_items' in sql.lower() or 'episodes' in sql.lower() for sql in statements)


def test_next_scan_replaces_disk_sets_visible_to_an_existing_worker(app, conn):
    seed_sources(conn)
    settings = replace(app.config['SETTINGS'], dry_run=False)
    assert scan(settings).status == 'finished'
    assert sources_view(conn, settings).disk_count == 5
    # The next refresh loses the vault disk; an already-open reader sees it.
    conn.execute("UPDATE media_items SET file_path=NULL WHERE plex_rating_key='300'")
    conn.commit()
    assert scan(settings).status == 'finished'
    assert sources_view(conn, settings) == SourcesView(2, '2021-01-01', 4)


def test_dry_run_does_not_replace_live_disk_sets_with_preview_facts(app, conn):
    seed_sources(conn)
    settings = replace(app.config['SETTINGS'], dry_run=False)
    assert scan(settings).status == 'finished'
    conn.execute("UPDATE media_items SET file_path=NULL WHERE plex_rating_key='300'")
    conn.commit()
    assert scan(replace(settings, dry_run=True)).status == 'finished'
    assert sources_view(conn, settings) == SourcesView(2, '2021-01-01', 5)
    assert conn.execute("SELECT disk FROM media_items WHERE plex_rating_key='300'").fetchone()[0] == 'disk3'
