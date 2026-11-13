"""Presence comes only from a complete section; absence never deletes history."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, episode, history_entry, movie, season, series
from almali_library_curator import library_scope
from almali_library_curator.config import Settings
from almali_library_curator.db import init_db
from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.models import UncuratedItemError
from almali_library_curator.plex import PlexClient, PlexError, PlexSync

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.fixture()
def scenario(tmp_path):
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Gone'), movie('101', 'Still here')]
    data.items['2'] = [series('200', 'Show', childCount=1, leafCount=2)]
    data.children['200'] = [season('210', '200', 1, leafCount=2)]
    data.children['210'] = [episode('300', '200', '210', 1), episode('301', '200', '210', 2)]
    data.history = [history_entry('999', 1, 1_700_000_000, 100, 100)]
    conn = init_db(tmp_path / 'curator.db')
    settings = Settings(config_dir=tmp_path, dry_run=False)
    with FakePlexServer(data) as server:
        client = PlexClient(server.base_url, 'test-token', rate_limit_per_second=0,
                            backoff_seconds=0, max_retries=0, page_size=1)
        PlexSync(client, conn).run()
        yield data, conn, settings, client
    conn.close()


def detect(conn, settings):
    return DetectionEngine(conn, settings=settings, clock=lambda: NOW).scan()


def stored(conn):
    return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
            for table in ('media_items', 'episodes', 'play_events', 'candidates',
                          'removal_requests', 'removal_events')}


def test_absent_movies_leave_detection_and_scope_without_erasing_history(scenario):
    data, conn, settings, client = scenario
    before = {c.plex_rating_key: c for c in detect(conn, settings)}
    assert {'100', '101'} <= before.keys()
    history = [tuple(row) for row in conn.execute('SELECT * FROM play_events')]
    data.items['1'] = data.items['1'][1:]
    PlexSync(client, conn).run()
    assert [tuple(row) for row in conn.execute('SELECT * FROM play_events')] == history
    assert '100' not in {c.plex_rating_key for c in detect(conn, settings)}
    assert '101' in {c.plex_rating_key for c in detect(conn, settings)}
    assert before['100'].media_item_id not in library_scope.curated_item_ids(conn, settings)
    assert not library_scope.item_is_curated(conn, settings, before['100'].media_item_id)
    selected = replace(settings, curated_libraries=('1',))
    assert library_scope.curated_item_ids(conn, selected) == frozenset({before['101'].media_item_id})
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 4
    engine = LifecycleEngine(conn, settings=settings, clock=lambda: NOW)
    snapshot = stored(conn)
    with pytest.raises(UncuratedItemError):
        engine.mark_for_removal(before['100'])
    assert stored(conn) == snapshot
    # Reappearance restores eligibility using the same row id, not a new item.
    data.items['1'].append(movie('100', 'Gone'))
    PlexSync(client, conn).run()
    assert {c.plex_rating_key: c.media_item_id for c in detect(conn, settings)}['100'] == before['100'].media_item_id


@pytest.mark.parametrize('state', ['marked', 'recheck_passed', 'removal_failed'])
def test_absent_request_closes_already_gone_before_due_date_and_keeps_history(scenario, state):
    data, conn, settings, client = scenario
    candidate = next(c for c in detect(conn, settings) if c.plex_rating_key == '100')
    engine = LifecycleEngine(conn, settings=settings, clock=lambda: NOW, collections=client)
    request = engine.mark_for_removal(candidate)
    assert any('100' in members for members in data.collection_items.values())
    conn.execute('UPDATE removal_requests SET state=? WHERE id=?', (state, request.id))
    conn.commit()
    data.items['1'] = []
    PlexSync(client, conn).run()
    report = engine.run_recheck()
    assert not report.errors and not report.passed
    assert [o.exit_code.value for o in report.cancelled] == ['already_gone']
    assert engine.open_requests() == []
    assert not any('100' in members for members in data.collection_items.values())
    assert data.deleted_media == []
    row = conn.execute('SELECT state, recheck_status, deleted_at FROM removal_requests').fetchone()
    assert tuple(row) == ('cancelled', 'already_gone', None)
    event = conn.execute('SELECT outcome, reason, freed_bytes FROM removal_events').fetchone()
    assert event['outcome'] == 'cancelled' and 'Plex' in event['reason']
    assert event['freed_bytes'] is None
    assert conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 4
    engine.run_recheck()
    assert conn.execute('SELECT COUNT(*) FROM removal_events').fetchone()[0] == 1


def test_failed_section_read_leaves_all_presence_and_history_unchanged(scenario):
    data, conn, settings, client = scenario
    before = stored(conn)
    data.items['1'] = []
    data.fail_always('/library/sections/2/all', 503)
    with pytest.raises(PlexError):
        PlexSync(client, conn).run()
    conn.rollback()
    assert stored(conn) == before
    assert {'100', '101'} <= {c.plex_rating_key for c in detect(conn, settings)}


@pytest.mark.parametrize('payloads', [
    [{'MediaContainer': {'totalSize': 2, 'size': 1, 'Metadata': [movie('101', 'Still here')]}},
     {'MediaContainer': {'totalSize': 2, 'size': 0}}],
    [{}],
    [{'MediaContainer': {'size': 0}}],
    [{'MediaContainer': {'totalSize': 0, 'size': 0, 'Metadata': 'broken'}}],
])
def test_partial_or_malformed_section_read_refuses_without_pruning(scenario, monkeypatch, payloads):
    _, conn, _, client = scenario
    before = stored(conn)
    answers = iter(payloads)
    original = client._request
    def request(path, params=None, **kwargs):
        if path == '/library/sections/1/all':
            return next(answers)
        return original(path, params, **kwargs)
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(PlexError):
        PlexSync(client, conn).run()
    conn.rollback()
    assert stored(conn) == before


@pytest.mark.parametrize('fallback', [False, True])
def test_incomplete_tv_children_prune_nothing_but_complete_reads_exclude_ghost_episodes(scenario, fallback):
    data, conn, settings, client = scenario
    data.bulk_reads_answer_nothing = fallback
    original = detect(conn, settings)
    original_size = next(c.size_bytes for c in original if c.plex_rating_key == '200')
    data.children['210'] = data.children['210'][:1]
    # Plex still advertises two episodes: not proof that the missing one is gone.
    PlexSync(client, conn).run()
    assert next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200') == original_size
    data.children['200'][0]['leafCount'] = 1
    data.items['2'][0]['leafCount'] = 1
    PlexSync(client, conn).run()
    assert next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200') == original_size // 2
    assert conn.execute('SELECT COUNT(*) FROM episodes').fetchone()[0] == 2


def test_contradictory_tv_totals_are_not_proof_of_absence(scenario):
    data, conn, settings, client = scenario
    size = next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200')
    data.children['200'] = []
    # The parent still advertises episodes even though its season count is zero.
    data.items['2'][0]['childCount'] = 0
    PlexSync(client, conn).run()
    assert next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200') == size


def test_absent_season_closes_only_its_request_not_a_surviving_series(scenario):
    data, conn, settings, client = scenario
    candidate = next(c for c in detect(conn, settings) if c.plex_rating_key == '200')
    engine = LifecycleEngine(conn, settings=settings, clock=lambda: NOW)
    request = engine.mark_for_removal(replace(candidate, unit=Unit.SEASONS, seasons=(1,)))
    data.children['200'] = []
    data.items['2'][0].update(childCount=0, leafCount=0)
    PlexSync(client, conn).run()
    report = engine.run_recheck()
    assert [(o.request.id, o.exit_code.value) for o in report.cancelled] == [(request.id, 'already_gone')]
    assert library_scope.item_is_curated(conn, settings, candidate.media_item_id)
    assert conn.execute('SELECT COUNT(*) FROM episodes').fetchone()[0] == 2


def test_series_moving_to_an_incomplete_section_keeps_its_unseen_episodes(scenario):
    data, conn, settings, client = scenario
    size = next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200')
    data.sections.append({'key': '3', 'title': 'Other shows', 'type': 'show'})
    data.items['3'] = data.items['2']
    data.items['2'] = []
    data.children['210'] = data.children['210'][:1]
    PlexSync(client, conn).run()
    assert next(c.size_bytes for c in detect(conn, settings) if c.plex_rating_key == '200') == size
    assert conn.execute(
        "SELECT l.plex_key FROM media_items m JOIN libraries l ON l.id=m.library_id"
        " WHERE m.plex_rating_key='200'"
    ).fetchone()[0] == '3'


def test_already_gone_dry_run_reports_without_mutating_requests(scenario):
    data, conn, settings, client = scenario
    candidate = next(c for c in detect(conn, settings) if c.plex_rating_key == '100')
    engine = LifecycleEngine(conn, settings=settings, clock=lambda: NOW)
    engine.mark_for_removal(candidate)
    data.items['1'] = []
    PlexSync(client, conn).run()
    before = stored(conn)
    preview = LifecycleEngine(conn, settings=replace(settings, dry_run=True), clock=lambda: NOW)
    report = preview.run_recheck()
    assert [o.exit_code.value for o in report.cancelled] == ['already_gone']
    assert stored(conn) == before
    assert data.deleted_media == []


def test_reusing_a_failed_sync_does_not_replay_observations(scenario):
    data, conn, settings, client = scenario
    sync = PlexSync(client, conn)
    data.fail_always('/library/sections/2/all', 503)
    with pytest.raises(PlexError):
        sync.run()
    data.always_fail.clear()
    data.items['1'] = []
    sync.run()
    assert not ({'100', '101'} & {c.plex_rating_key for c in detect(conn, settings)})
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 4


def test_scope_storage_and_file_enrichment_ignore_absent_rows(scenario):
    from almali_library_curator.enrichment.files import refresh_files
    from almali_library_curator.scan.snapshot import scope_storage
    data, conn, settings, client = scenario
    candidate = next(c for c in detect(conn, settings) if c.plex_rating_key == '200')
    data.items['1'] = data.items['1'][1:]
    data.children['210'] = data.children['210'][:1]
    data.children['200'][0]['leafCount'] = 1
    data.items['2'][0]['leafCount'] = 1
    PlexSync(client, conn).run()
    assert scope_storage(conn, candidate.media_item_id, Unit.SERIES) == (2 * 1024**3, None)
    counts = refresh_files(conn, None, settings)
    assert (counts.movies, counts.episodes) == (1, 1)
    # The default/no-settings entry point follows the same presence rule.
    counts = refresh_files(conn, None)
    assert (counts.movies, counts.episodes) == (1, 1)


def test_unread_and_uncurated_sections_are_not_pruned(scenario):
    data, conn, settings, client = scenario
    data.items['1'] = []
    PlexSync(client, conn, curated_libraries=('2',)).run()
    assert {'100', '101'} <= {c.plex_rating_key for c in detect(conn, settings)}
    data.sections = [s for s in data.sections if s['key'] != '1']
    PlexSync(client, conn).run()
    assert {'100', '101'} <= {c.plex_rating_key for c in detect(conn, settings)}
