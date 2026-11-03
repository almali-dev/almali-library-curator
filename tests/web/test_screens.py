"""The six T9 screens rendered through the Flask client (S7)."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from markupsafe import Markup
import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import make_candidate, seed_item, seed_library, seed_user
from almali_library_curator.lifecycle import LifecycleEngine

ROOT = Path(__file__).resolve().parents[2] / 'almali_library_curator'
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000
SCREENS = ('/', '/?tab=leaving_soon', '/?tab=protected', '/history', '/connections', '/settings', '/setup')


def row(identifier, title, size, *, unit='movie', seasons=(), categories=('never_watched',), **extra):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=list(seasons),
                type_badge=unit.upper(), categories=list(categories), reasons=['A reason.'],
                reason='A reason.', size_bytes=size, disk='disk3', rating=7.4, rating_source='IMDb', **extra)


def snapshot(app, candidates, protected=(), *, dry_run=False):
    settings = replace(app.config['SETTINGS'], dry_run=dry_run)
    save_settings(settings)

    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=len(candidates), candidate_rows=list(candidates),
                                          protected_rows=list(protected), protected=len(protected))}
    return ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def forms(response):
    """(action, {hidden field: value}, button label) for every POST form."""
    html = response.get_data(as_text=True)
    found = []
    for action, body in re.findall(r'<form method="post" action="([^"]+)">(.*?)</form>', html, re.S):
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))
        label = ' '.join(Markup(body).striptags().split())
        # Actions keep the page language as a query; compare the route only.
        found.append((action.replace('&amp;', '&').split('?')[0], fields, label))
    return found


def test_library_health_shows_the_summary_cards_tabs_filters_and_rows(app, client):
    snapshot(app, [row(1, 'Tenet 2020', 142 * GB),
                   row(2, 'Lost in Space S2–S3', 291 * GB, unit='seasons', seasons=(2, 3),
                       categories=('stalled_series',))],
             [row(3, 'Heat', 385 * GB, protection_kind='rating', protection_reason='Rated 8.3.')])
    response = client.get('/?lang=en')
    body = text(response)
    assert response.status_code == 200
    # The "//" label prefix is the HUD stylesheet's (.section-title::before).
    assert response.get_data(as_text=True).count('class="section-title"') >= 5
    for expected in ('Candidates 433 GB 2 items you could remove',
                     'Leaving soon 0 B 0 in grace · 14-day period',
                     'Protected 385 GB 1 item · rating',
                     'Freed so far 0 B 0 items',
                     'Candidates · 2', 'Leaving soon · 0', 'Protected · 1',
                     'All 2', 'Stalled series 1', 'Never watched 1', 'Seasons everyone finished 0',
                     'Everything that matches at least one category, largest first.'):
        assert expected in body, expected
    # Largest first, with type, title with unit, reason, size, disk, rating source.
    assert body.index('Lost in Space S2–S3') < body.index('Tenet 2020')
    assert 'Seasons Lost in Space S2–S3 A reason.' in body
    assert '291 GB' in body and 'disk3' in body and '7.4 IMDb' in body
    actions = forms(response)
    assert ('/items/2/mark', {'unit': 'seasons', 'seasons': '2,3'}, 'Mark for removal') in actions
    assert ('/items/2/keep-forever', {'unit': 'seasons', 'seasons': '2,3'}, 'Keep forever') in actions
    assert ('/actions/scan', {}, 'Run scan now') in actions


def test_category_filter_lists_only_matching_rows(app, client):
    snapshot(app, [row(1, 'Tenet 2020', 142 * GB),
                   row(2, 'Foundation S1', 90 * GB, unit='seasons', seasons=(1,), categories=('stalled_series',))])
    body = text(client.get('/?category=stalled_series'))
    assert 'Foundation S1' in body and 'Tenet 2020' not in body


def test_sizes_and_last_scan_follow_the_locale(app, client):
    snapshot(app, [row(1, 'Tenet 2020', 1_310_000_000_000)])
    assert '1,31 TB' in text(client.get('/?lang=ca'))
    assert '1.31 TB' in text(client.get('/?lang=en'))
    english = text(client.get('/?lang=en'))
    assert 'Last scan · ' in english and 'Not run yet' not in english


def test_a_dry_run_snapshot_is_labelled_as_what_would_happen(app, client):
    snapshot(app, [row(1, 'Tenet 2020', GB)], dry_run=True)
    assert 'Dry run · would happen' in text(client.get('/'))


def test_a_marked_candidate_offers_undo_instead_of_mark(app, client, conn):
    library = seed_library(conn, '1', 'Movies')
    identifier = seed_item(conn, library, '11', 'Tenet')
    request = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: datetime.now(timezone.utc)).mark_for_removal(
        make_candidate(identifier, '11', 'Tenet'), marked_by='owner')
    snapshot(app, [row(identifier, 'Tenet', GB)])
    actions = [(action, label) for action, _, label in forms(client.get('/'))]
    assert (f'/requests/{request.id}/undo', 'Undo') in actions
    assert (f'/items/{identifier}/mark', 'Mark for removal') not in actions


def test_leaving_soon_shows_days_left_keep_it_and_cancelled_reasons(app, client, conn):
    library = seed_library(conn, '1', 'Movies')
    first = seed_item(conn, library, '11', 'Tenet')
    second = seed_item(conn, library, '12', 'Heat')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: datetime.now(timezone.utc))
    kept = engine.mark_for_removal(make_candidate(first, '11', 'Tenet'), marked_by='owner')
    cancelled = engine.mark_for_removal(make_candidate(second, '12', 'Heat'), marked_by='owner')
    engine.keep_it(cancelled)
    response = client.get('/?tab=leaving_soon')
    body = text(response)
    assert 'Tenet' in body and '14 days left' in body and 'Marked' in body
    assert 'role="progressbar"' in response.get_data(as_text=True)
    assert (f'/requests/{kept.id}/keep', {}, 'Keep it') in forms(response)
    assert 'Removal cancelled: the owner chose to keep it.' in body


def test_protected_tab_shows_what_protects_and_undo_only_for_keep_forever(app, client):
    snapshot(app, [], [row(3, 'Heat', GB, protection_kind='rating', protection_reason='Rated 8.3.'),
                       row(4, 'Up', GB, protection_kind='keep_forever', protection_reason='Kept forever by hand.')])
    response = client.get('/?tab=protected')
    body = text(response)
    assert 'Rated 8.3.' in body and 'Kept forever by hand.' in body
    labels = [(action, label) for action, _, label in forms(response)]
    assert ('/items/4/undo-keep-forever', 'Undo keep forever') in labels
    assert not any(action.startswith('/items/3/') for action, _ in labels)


def test_history_lists_events_with_filters_monthly_freed_and_csv_link(client, conn):
    for outcome, month, freed, title in (('deleted', '2026-07', 2 * GB, 'Tenet'),
                                         ('cancelled', '2026-10', None, 'Heat')):
        conn.execute('INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes, disk,'
                     ' reason, marked_by, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (outcome, title, 'movie', 2 * GB, freed, 'disk3', 'A reason', 'owner',
                      month + '-01T12:00:00+00:00'))
    conn.commit()
    response = client.get('/history?lang=en')
    body = text(response)
    html = response.get_data(as_text=True)
    assert body.index('Heat') < body.index('Tenet')
    assert 'July 2026 2 GB' in body
    assert 'href="/history.csv?' in html and 'Export CSV' in body
    filtered = text(client.get('/history?outcome=deleted'))
    assert 'Tenet' in filtered and 'Heat' not in filtered


def test_connections_page_reads_configuration_without_probing(app, client, monkeypatch):
    import almali_library_curator.web.views as views
    monkeypatch.setattr(views, '_probes', lambda settings: pytest.fail('probed on a plain page load'))
    # Secrets never reach the settings file; they come from the environment.
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'k')
    body = text(client.get('/connections'))
    assert 'Sonarr Configured' in body and 'Radarr Not configured' in body and 'Test connections' in body
    assert 'k' not in re.findall(r'value="([^"]*)"', client.get('/connections').get_data(as_text=True))


def test_connections_test_runs_the_probes(app, client, monkeypatch):
    import almali_library_curator.web.views as views
    monkeypatch.setattr(views, '_probes', lambda settings: {name: (lambda: True) for name in
                                                            ('plex', 'sonarr', 'radarr', 'tautulli', 'tmdb', 'host')})
    # Secrets never reach the settings file; they come from the environment.
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'k')
    assert 'Sonarr Connected' in text(client.get('/connections?test=1'))


def test_settings_page_shows_editable_values_and_never_a_secret_field(app, client):
    save_settings(replace(app.config['SETTINGS'], plex_token='secret-token', grace_days=21))
    response = client.get('/settings')
    html = response.get_data(as_text=True)
    assert 'name="grace_days"' in html and 'value="21"' in html
    assert 'name="scan_time"' in html and 'name="collection_name"' in html
    assert 'name="plex_token"' not in html and 'secret-token' not in html
    assert ('/settings/kill-switch', {'enabled': 'on'}, 'Stop all deletions') in forms(response)


def test_invalid_settings_re_render_the_settings_screen_with_the_error(client):
    response = client.post('/settings?lang=en', data={'grace_days': '2'})
    assert response.status_code == 400
    body = text(response)
    assert 'Grace period must be between 3 and 60 days.' in body and 'name="grace_days"' in response.get_data(as_text=True)


def test_first_run_wizard_lists_libraries_to_protect(client, conn):
    seed_library(conn, '7', 'Filmoteca')
    response = client.get('/setup?step=libraries')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="protected_libraries" value="7"' in html and 'Filmoteca' in text(response)


def test_wizard_offers_its_three_steps(client):
    body = text(client.get('/setup'))
    for step in ('Plex', 'Libraries', 'Optional connections'):
        assert step in body


def test_flash_messages_are_translated(app, client):
    response = client.post('/settings/kill-switch?lang=ca', data={'enabled': 'on'}, follow_redirects=True)
    body = text(response)
    assert 'web.flash' not in body
    assert json.loads((ROOT / 'i18n/ca.json').read_text())['web.flash.kill_switch_on'] in body


def test_sources_panel_lists_synced_facts(client, conn):
    for index in range(3):
        seed_user(conn, str(index), f'user{index}')
    assert 'Plex · 3 users' in text(client.get('/'))


@pytest.mark.parametrize('locale', ['en', 'ca', 'es'])
@pytest.mark.parametrize('path', SCREENS)
def test_every_screen_renders_without_raw_translation_keys(app, client, locale, path):
    snapshot(app, [row(1, 'Tenet 2020', GB)], [row(3, 'Heat', GB, protection_kind='rating', protection_reason='R')])
    response = client.get(path + ('&' if '?' in path else '?') + 'lang=' + locale)
    assert response.status_code == 200
    body = text(response)
    keys = json.loads((ROOT / 'i18n/en.json').read_text()).keys()
    assert not [key for key in keys if key in body]
    assert 'http://' not in response.get_data(as_text=True) and 'https://' not in response.get_data(as_text=True)


def test_screen_templates_have_no_literal_user_facing_copy():
    for path in (ROOT / 'web/templates').rglob('*.html'):
        template = re.sub(r'\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}', '', path.read_text(), flags=re.S)
        assert not re.search(r'[^\W\d_]', ''.join(Markup(template).striptags())), path


def test_last_scan_time_is_shown_in_local_time(app, client, monkeypatch):
    import time
    monkeypatch.setenv('TZ', 'Europe/Madrid')
    time.tzset()
    try:
        snapshot(app, [row(1, 'Tenet 2020', GB)])  # finished at 06:00 UTC = 07:00 in Madrid
        assert 'Last scan · 20 November 07:00' in text(client.get('/?lang=en'))
    finally:
        monkeypatch.undo()
        time.tzset()
