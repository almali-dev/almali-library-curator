"""Alternate and failure-path characterization over real public boundaries."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import make_candidate, seed_item, seed_library, seed_episode


def keys(client):
    with client.session_transaction() as session:
        return [key for _, key in session.get('_flashes', [])]


def test_mark_uses_tmdb_fallback_rating_protection(app, client, conn, monkeypatch):
    from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie
    data = FakeTmdbDataset()
    data.movies[550] = tmdb_movie(550, 'Tenet', vote_average=8.9)
    monkeypatch.setenv('ALC_TMDB_API_TOKEN', data.access_token)
    with FakeTmdbServer(data) as server:
        save_settings(replace(app.config['SETTINGS'], tmdb_base_url=server.base_url))
        library = seed_library(conn, '1', 'Movies')
        item = seed_item(conn, library, '11', 'Tenet')
        conn.execute('UPDATE media_items SET tmdb_id=550 WHERE id=?', (item,))
        conn.commit()
        response = client.post(f'/items/{item}/mark', data={'unit': 'movie'})
        assert response.status_code == 303
        assert keys(client)[-1] == 'web.flash.protected'
        assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
        assert server.requests[0]['path'] == '/3/movie/550'


def test_scan_start_failure_never_flashes_success_or_error_body(app, client, caplog):
    class BrokenRunner:
        def start_background(self):
            raise RuntimeError('sensitive-external-body')
    app.extensions['scan_runner'] = BrokenRunner()
    assert client.post('/actions/scan').status_code == 303
    assert keys(client)[-1] == 'web.flash.action_failed'
    assert 'sensitive-external-body' not in caplog.text


def test_plex_collection_failure_keeps_durable_request_without_false_success(app, client, conn, monkeypatch, caplog):
    from fake_plex_server import FakePlexDataset, FakePlexServer
    data = FakePlexDataset()
    data.fail_always('/library/sections/1/collections', 401)
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'test-token')
    with FakePlexServer(data) as server:
        save_settings(replace(app.config['SETTINGS'], dry_run=False, plex_base_url=server.base_url,
                              plex_rate_limit_per_second=1000))
        library = seed_library(conn, '1', 'Movies')
        item = seed_item(conn, library, '11', 'Tenet', rating=7.0)
        assert client.post(f'/items/{item}/mark', data={'unit': 'movie'}).status_code == 303
        assert keys(client)[-1] == 'web.flash.action_failed'
        assert conn.execute('SELECT state FROM removal_requests').fetchone()[0] == 'marked'
        assert 'test-token' not in caplog.text


def test_damaged_scope_is_isolated_on_library_route_too(app, client, conn):
    from flask import template_rendered
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: datetime.now(timezone.utc))
    engine.mark_for_removal(make_candidate(item, '11', 'Tenet'))
    conn.execute("UPDATE candidates SET facts='broken-json'")
    conn.commit()
    models = []
    def record(sender, template, context, **kwargs):
        models.append(context['model'])
    with template_rendered.connected_to(record, app):
        response = client.get('/?tab=leaving_soon')
    assert response.status_code == 200 and models[0].leaving_soon.items[0].unreadable


def test_season_scope_uses_only_its_size_disk_title_and_protection(app, client, conn):
    from almali_library_curator.web.views import leaving_soon_view
    library = seed_library(conn, '2', 'Shows')
    item = seed_item(conn, library, '20', 'Foundation', item_type='series')
    seed_episode(conn, '20', 1, 1)
    seed_episode(conn, '20', 2, 1)
    conn.execute("UPDATE episodes SET file_size_bytes=10, disk='disk1' WHERE season_number=1")
    conn.execute("UPDATE episodes SET file_size_bytes=20, disk='disk2' WHERE season_number=2")
    conn.commit()
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: datetime.now(timezone.utc))
    engine.mark_for_removal(make_candidate(item, '20', 'Foundation S2', unit=Unit.SEASONS, seasons=(2,)))
    row = leaving_soon_view(conn, app.config['SETTINGS']).items[0]
    assert row.title == 'Foundation S2' and row.size_bytes == 20 and row.disk == 'disk2'
    assert client.post(f'/items/{item}/keep-forever', data={'unit': 'seasons', 'seasons': '2'}).status_code == 303
    assert conn.execute('SELECT detail FROM protections').fetchone()[0] == 'seasons:2'
    assert client.post(f'/items/{item}/undo-keep-forever', data={'unit': 'seasons', 'seasons': '2'}).status_code == 303
    assert conn.execute('SELECT count(*) FROM protections').fetchone()[0] == 0


def test_failed_newer_scan_does_not_replace_latest_finished_items(app, conn):
    from almali_library_curator.web.views import candidates_view
    from test_views import snapshot, displayed
    run = snapshot(app, [displayed(1, 'Tenet', 10)])
    def factory(*args):
        def broken():
            raise ValueError('private')
        return {name: broken for name in ('sync', 'enrichment', 'detection', 'recheck', 'deletion')}
    assert ScanRunner(app.config['SETTINGS'], steps_factory=factory).run('manual').status == 'failed'
    model = candidates_view(conn)
    assert model.last_scan['id'] == run.id and model.items[0].title == 'Tenet'


@pytest.mark.parametrize('service', ['sonarr', 'radarr', 'tautulli', 'tmdb'])
def test_real_optional_probes_use_their_read_endpoints(app, conn, service):
    from almali_library_curator.web.views import connections_view
    from fake_sonarr_server import FakeSonarrServer
    from fake_radarr_server import FakeRadarrServer
    from fake_tautulli_server import FakeTautulliServer
    from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie
    tmdb = FakeTmdbDataset()
    tmdb.movies[550] = tmdb_movie(550, 'Film')
    servers = {'sonarr': FakeSonarrServer(), 'radarr': FakeRadarrServer(),
               'tautulli': FakeTautulliServer(), 'tmdb': FakeTmdbServer(tmdb)}
    credential = 'tmdb_api_token' if service == 'tmdb' else f'{service}_api_key'
    token = tmdb.access_token if service == 'tmdb' else 'test-api-key'
    with servers[service] as server:
        settings = replace(app.config['SETTINGS'], **{f'{service}_base_url': server.base_url, credential: token})
        model = connections_view(conn, settings)
        assert next(row for row in model.services if row.name == service).status == 'connected'
        assert server.requests


def test_host_paths_are_checked_with_is_mounted(app, conn, tmp_path):
    from almali_library_curator.web.views import connections_view
    settings = replace(app.config['SETTINGS'], media_root=str(tmp_path), unraid_root='')
    model = connections_view(conn, settings)
    assert next(row for row in model.services if row.name == 'host').status == 'connected'
    model = connections_view(conn, replace(settings, media_root=str(tmp_path / 'absent')))
    assert next(row for row in model.services if row.name == 'host').status == 'unreachable'


def test_a_probe_that_answers_false_is_unreachable_and_the_rest_still_answer(app, conn):
    """One card saying no never hides the others, and it is read as a no (#144)."""
    from almali_library_curator.web.views import connections_view
    settings = replace(app.config['SETTINGS'], plex_base_url='http://plex.example:32400',
                       plex_token='x', media_root='')
    probes = {name: (lambda: False) for name in ('plex', 'sonarr', 'radarr', 'tautulli', 'tmdb', 'host')}
    model = connections_view(conn, settings, probes=probes)
    # False is an answer, not an error: the card is unreachable, never connected.
    assert next(row for row in model.services if row.name == 'plex').status == 'unreachable'
    # Nothing else was configured, so nothing else was probed or reported.
    assert [row.status for row in model.services if row.name != 'plex'] == ['not_configured'] * 5
