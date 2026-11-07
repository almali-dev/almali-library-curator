"""S15 through the public screens.

Rule 2: both deletion switches are editable in Settings.
Rule 5: a configured arr whose deletion switch is off is always flagged,
wherever an item of its unit type appears, because it may download the
item again.
Rule 8: Plex is the only required connection.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from markupsafe import Markup
import pytest

from almali_library_curator.config import load_settings, save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import FakeCollections, make_candidate, seed_item, seed_library

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def warning(service, locale='en'):
    return ' '.join(Markup(translate('removal.warning.arr_redownload', locale)
                           .format(service=service)).striptags().split())


def configure(app, monkeypatch, **switches):
    """Radarr and Sonarr configured (secrets come from the environment)."""
    for name in ('RADARR', 'SONARR'):
        monkeypatch.setenv(f'ALC_{name}_BASE_URL', f'http://{name.lower()}.invalid')
        monkeypatch.setenv(f'ALC_{name}_API_KEY', 'key')
    settings = replace(app.config['SETTINGS'], **switches)
    save_settings(settings)
    return settings


def scan_with(app, settings, candidates):
    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=len(candidates), candidate_rows=candidates,
                                          protected_rows=[], protected=0)}
    ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')


def row(identifier, title, unit):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=[], type_badge=unit.upper(),
                categories=['never_watched'], reasons=['A reason.'], reason='A reason.',
                size_bytes=10, disk='disk1', rating=7.0, rating_source='IMDb')


# -- rule 5 ----------------------------------------------------------------------


def test_a_switched_off_radarr_is_flagged_on_every_movie_row(app, client, conn, monkeypatch):
    settings = configure(app, monkeypatch, radarr_deletes=False)
    library = seed_library(conn, '1', 'Movies')
    marked = seed_item(conn, library, '11', 'Tenet')
    LifecycleEngine(conn, settings=settings, clock=lambda: NOW,
                    collections=FakeCollections()).mark_for_removal(make_candidate(marked, '11', 'Tenet'))
    conn.commit()
    scan_with(app, settings, [row(marked, 'Tenet', 'movie'), row(99, 'Some Show', 'series')])

    candidates = text(client.get('/'))
    leaving = text(client.get('/?tab=leaving_soon'))
    assert candidates.count(warning('Radarr')) == 1
    assert warning('Sonarr') not in candidates
    assert leaving.count(warning('Radarr')) == 1


def test_no_warning_when_the_arr_is_absent_or_its_switch_is_on(app, client, conn, monkeypatch):
    settings = configure(app, monkeypatch)
    scan_with(app, settings, [row(1, 'Tenet', 'movie'), row(2, 'Some Show', 'series')])
    page = text(client.get('/'))
    assert warning('Radarr') not in page and warning('Sonarr') not in page


def test_a_switched_off_sonarr_is_flagged_in_the_readers_language(app, client, conn, monkeypatch):
    settings = configure(app, monkeypatch, sonarr_deletes=False)
    scan_with(app, settings, [row(2, 'Some Show', 'series')])
    page = text(client.get('/?lang=ca'))
    assert warning('Sonarr', 'ca') in page
    assert warning('Sonarr', 'ca') != warning('Sonarr', 'en')


# -- rule 2 ----------------------------------------------------------------------


def test_settings_offers_both_switches_and_saves_them(app, client):
    page = client.get('/settings').get_data(as_text=True)
    assert 'name="radarr_deletes"' in page and 'name="sonarr_deletes"' in page
    form = {'radarr_deletes': 'false', 'sonarr_deletes': 'true'}
    response = client.post('/settings', data=form)
    assert response.status_code in (200, 302, 303)
    saved = load_settings(app.config['SETTINGS'].config_dir, env={})
    assert (saved.radarr_deletes, saved.sonarr_deletes) == (False, True)


def test_an_invalid_switch_value_is_refused_and_nothing_changes(app, client):
    response = client.post('/settings', data={'radarr_deletes': 'maybe'})
    assert response.status_code == 400
    saved = load_settings(app.config['SETTINGS'].config_dir, env={})
    assert saved.radarr_deletes is True


# -- rule 8 ----------------------------------------------------------------------


def test_the_wizard_cannot_be_completed_without_plex(client):
    plex_step = client.get('/setup?step=plex').get_data(as_text=True)
    assert 'step=libraries' not in plex_step.split('wizard-nav', 1)[1]
    assert translate('setup.plex_required', 'en') in text(client.get('/setup?step=plex'))
    last = client.get('/setup?step=connections').get_data(as_text=True)
    assert translate('setup.finish', 'en') not in last.split('wizard-nav', 1)[1]


def test_the_wizard_moves_on_once_plex_is_configured(app, client, monkeypatch):
    monkeypatch.setenv('ALC_PLEX_BASE_URL', 'http://plex.invalid:32400')
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'token')
    plex_step = client.get('/setup?step=plex').get_data(as_text=True)
    assert 'step=libraries' in plex_step.split('wizard-nav', 1)[1]
    last = client.get('/setup?step=connections').get_data(as_text=True)
    assert translate('setup.finish', 'en') in last.split('wizard-nav', 1)[1]


@pytest.mark.parametrize('path', ['/connections', '/setup?step=plex'])
def test_connections_mark_plex_as_the_required_connection(client, path):
    page = text(client.get(path))
    assert page.count(translate('connections.required', 'en')) == 1


def test_health_reports_not_ready_without_plex(client):
    response = client.get('/healthz')
    payload = response.get_json()
    # The database is ready, so the container stays alive (status code and
    # status unchanged); readiness says what is still missing.
    assert (response.status_code, payload['status']) == (200, 'ok')
    assert (payload['ready'], payload['missing']) == (False, ['plex'])


def test_health_reports_ready_with_plex(client, monkeypatch):
    monkeypatch.setenv('ALC_PLEX_BASE_URL', 'http://plex.invalid:32400')
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'token')
    payload = client.get('/healthz').get_json()
    assert (payload['ready'], payload['missing']) == (True, [])
