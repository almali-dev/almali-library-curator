"""Service probes isolate failures, and sidebar counts come from synced facts."""
from dataclasses import replace

import pytest


@pytest.mark.parametrize('service', ['plex', 'sonarr', 'radarr', 'tautulli', 'tmdb', 'host'])
def test_unreachable_service_does_not_raise_or_expose_diagnostics(app, conn, service):
    from almali_library_curator.web.views import connections_view
    settings = replace(app.config['SETTINGS'], plex_base_url='http://localhost', plex_token='x',
                       sonarr_base_url='http://localhost', sonarr_api_key='x',
                       radarr_base_url='http://localhost', radarr_api_key='x',
                       tautulli_base_url='http://localhost', tautulli_api_key='x', tmdb_api_token='x')
    def healthy():
        return object()
    def broken():
        raise RuntimeError('never-show-this-secret')
    probes = {name: healthy for name in ('plex', 'sonarr', 'radarr', 'tautulli', 'tmdb', 'host')}
    probes[service] = broken
    model = connections_view(conn, settings, probes=probes)
    assert next(row for row in model.services if row.name == service).status == 'unreachable'
    assert all(row.status == 'connected' for row in model.services if row.name != service)
    assert 'never-show-this-secret' not in repr(model)


def test_unconfigured_optional_services_are_not_probed(app, conn):
    from almali_library_curator.web.views import connections_view
    called = []
    probes = {name: lambda: called.append(True) for name in ('plex', 'sonarr', 'radarr', 'tautulli', 'tmdb')}
    model = connections_view(conn, replace(app.config['SETTINGS'], media_root=''), probes=probes)
    assert called == []
    assert all(row.status == 'not_configured' for row in model.services)


def test_sources_counts_users_first_tautulli_play_and_distinct_disks(app, conn):
    from almali_library_curator.web.views import sources_view
    conn.execute("INSERT INTO users (plex_user_id, title) VALUES ('1', 'Marc')")
    conn.execute("INSERT INTO play_events (user_id, plex_rating_key, source, played_at)"
                 " VALUES (1, '11', 'tautulli', '2021-04-01T12:00:00+00:00')")
    conn.execute("INSERT INTO media_items (plex_rating_key, title, item_type, disk) VALUES ('11', 'Film', 'movie', 'disk3')")
    conn.execute("INSERT INTO episodes (plex_rating_key, series_rating_key, disk) VALUES ('12', '20', 'disk3')")
    conn.execute("INSERT INTO episodes (plex_rating_key, series_rating_key, disk) VALUES ('13', '20', 'disk4')")
    conn.commit()
    model = sources_view(conn)
    assert model.plex_users == 1 and model.tautulli_since.startswith('2021')
    assert model.disk_count == 2


def test_real_plex_connection_probe_uses_sections(app, conn):
    from almali_library_curator.web.views import connections_view
    from fake_plex_server import FakePlexServer
    with FakePlexServer() as server:
        settings = replace(app.config['SETTINGS'], plex_base_url=server.base_url, plex_token='test-token', media_root='')
        model = connections_view(conn, settings)
        assert next(row for row in model.services if row.name == 'plex').status == 'connected'
        assert any(row['path'] == '/library/sections' for row in server.requests)
