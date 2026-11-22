"""S3-S6 (T1): the first-run checklist, the header status and the empty states.

The owner deployed v0.40.0, opened the app and could not tell whether it was
configured: the Library page showed nothing while Connections showed a
connected Plex. These tests read the pages the owner reads, through the public
HTTP interface, and assert the words of the catalogs rather than markup order.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from html import unescape
import re

import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.i18n import SUPPORTED_LOCALES, load_catalogs, translate
from almali_library_curator.scan import ScanRunner
from almali_library_curator.scan.lock import ScanLock
from test_views import displayed, snapshot

GB = 1_000_000_000
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
PLEX_ADDRESS = 'http://plex.example:32400'
PLEX_TOKEN = 'plex-token'
PLEX_HOST = 'plex.example:32400'

# Every string this unit adds: one key must exist, non-empty, in the three
# catalogs (S6), and no page may ever print one of them raw.
FIRST_RUN_KEYS = (
    'scan.next',
    'scan.status.never',
    'scan.status.never_hint',
    'state.real_deletion',
    'action.scan_now',
    'action.connect_plex',
    'action.setup_libraries',
    'action.setup_connections',
    'action.open_settings',
    'summary.pending_value',
    'summary.pending',
    'empty.candidates_never_title',
    'empty.candidates_never',
    'empty.candidates_never_note',
    'empty.candidates_never_running',
    'setup.checklist.progress',
    'setup.checklist.progress_label',    'setup.checklist.remaining.plex',
    'setup.checklist.remaining.analysis',
    'setup.checklist.remaining.both',
    'setup.checklist.plex.done',
    'setup.checklist.plex.missing',
    'setup.checklist.analysis.pending',
    'setup.checklist.analysis.next',
    'setup.checklist.analysis.running',
    'setup.checklist.analysis.failed',
    'setup.checklist.analysis.failed_step',
    'setup.checklist.analysis.finished',
    'setup.checklist.libraries.known',
    'setup.checklist.libraries.pending',
    'setup.checklist.connections.intro',
    'setup.checklist.option.tautulli',
    'setup.checklist.option.sonarr',
    'setup.checklist.option.radarr',
    'setup.checklist.option.tmdb',
    'setup.checklist.option.host',
    'setup.checklist.go_live.dry_run',
    'setup.checklist.go_live.real',
    'setup.state.done',
    'setup.state.pending',
    'setup.state.running',
    'setup.state.failed',
    'setup.state.optional',
    'setup.step.analysis',
    'setup.step.go_live',
)


def label(key, locale='en', **params):
    """The catalog string of ``key``, formatted as the page formats it."""
    value = translate(key, locale)
    return value.format(**params) if params else value


def page(response):
    """The visible words of a page, entities decoded, one space between them."""
    text = unescape(re.sub(r'<[^>]+>', ' ', response.get_data(as_text=True)))
    return ' '.join(text.split())


def html(response):
    return response.get_data(as_text=True)


def header_last_scan(markup):
    """The words the header prints after 'Last scan · '."""
    inner = re.search(r'<p class="last-scan">(.*?)</p>', markup, re.S).group(1)
    words = ' '.join(unescape(re.sub(r'<[^>]+>', ' ', inner)).split())
    return words.split('·', 1)[1].strip()


@pytest.fixture
def plex(monkeypatch):
    """A Plex connection configured through the environment, as an install has."""
    monkeypatch.setenv('ALC_PLEX_BASE_URL', PLEX_ADDRESS)
    monkeypatch.setenv('ALC_PLEX_TOKEN', PLEX_TOKEN)


def failed_scan(app):
    """A recorded failed run, the way the owner's own run was recorded."""
    def factory(*args):
        steps = {name: (lambda: {}) for name in
                 ('enrichment', 'detection', 'recheck', 'deletion')}
        steps['sync'] = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        return steps
    return ScanRunner(app.config['SETTINGS'], steps_factory=factory, clock=lambda: NOW).run('manual')


# -- S3: the checklist on the home page ---------------------------------------


def test_the_checklist_appears_when_plex_is_not_configured(client):
    body = page(client.get('/?lang=en'))
    assert label('setup.title') in body
    for step in ('plex', 'analysis', 'libraries', 'connections', 'go_live'):
        assert label(f'setup.step.{step}'), step
    assert label('setup.checklist.remaining.both') in body
    assert label('setup.checklist.plex.missing') in body
    assert label('action.connect_plex') in body


def test_the_checklist_leads_to_the_plex_connection_when_it_is_missing(client):
    markup = html(client.get('/?lang=en'))
    assert '<a class="btn" href="/setup?step=plex' in markup


def test_a_never_analysed_plex_install_asks_for_the_first_analysis(app, client, plex):
    response = client.get('/?lang=en')
    body = page(response)
    # Step 1 is done and names the server the app is connected to.
    assert label('setup.checklist.plex.done', server=PLEX_HOST) in body
    assert label('setup.state.done') in body
    # Step 2 is the next thing to do: run it now, or wait for 06:00.
    assert label('setup.state.pending') in body
    assert label('action.scan_now') in body
    assert label('setup.checklist.analysis.next', time='06:00') in body
    assert label('setup.checklist.remaining.analysis') in body
    # The action posts, exactly as the header's run button does.
    assert 'action="/actions/scan' in html(response)


def test_a_running_analysis_says_so_in_the_checklist(app, client, plex):
    lock = ScanLock(app.config['SETTINGS'].config_dir)
    assert lock.acquire()
    try:
        body = page(client.get('/?lang=en'))
    finally:
        lock.release()
    assert label('setup.state.running') in body
    assert label('setup.checklist.analysis.running') in body
    # Nothing is offered to start twice while it runs.
    assert label('action.scan_now') not in body


def test_a_failed_analysis_shows_its_error_and_a_retry(app, client, plex):
    assert failed_scan(app).status == 'failed'
    body = page(client.get('/?lang=en'))
    assert label('setup.state.failed') in body
    assert label('setup.checklist.analysis.failed_step', step=label('scan.step.sync')) in body
    assert label('action.scan_now') in body


def test_a_finished_analysis_dates_the_second_step(app, client):
    """Plex is still open here, so the checklist stays and dates step 2."""
    snapshot(app, [displayed(1, 'Tenet', 10 * GB)])
    markup = html(client.get('/?lang=en'))
    body = page(client.get('/?lang=en'))
    # One date for both: the step repeats the label of the header, not another.
    when = header_last_scan(markup)
    assert label('setup.checklist.analysis.finished', when=when) in body
    assert label('setup.state.done') in body
    assert label('setup.checklist.remaining.plex') in body


def test_the_checklist_goes_away_once_plex_and_a_finished_analysis_exist(app, client, plex):
    snapshot(app, [displayed(1, 'Tenet', 10 * GB)])
    body = page(client.get('/?lang=en'))
    assert label('setup.title') not in body
    assert label('action.scan_now') not in body
    assert label('setup.state.optional') not in body


def test_an_optional_connection_never_keeps_the_checklist_on_screen(app, client, plex):
    """S3: optional steps never keep it on screen; only the required ones do."""
    # Nothing optional is configured here, and the checklist is already hidden.
    snapshot(app, [])
    assert label('setup.title') not in page(client.get('/?lang=en'))


def test_the_optional_connections_list_their_state_and_what_they_add(app, client, plex, monkeypatch):
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr.invalid:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'key')
    markup = html(client.get('/?lang=en'))
    body = page(client.get('/?lang=en'))
    assert label('setup.checklist.connections.intro') in body
    for name in ('tautulli', 'sonarr', 'radarr', 'tmdb', 'host'):
        assert label(f'setup.checklist.option.{name}') in body, name
    assert 'Sonarr' in body and 'Tautulli' in body and 'Radarr' in body and 'TMDB' in body
    assert label('connections.host') in body
    # Each service says its own state, by icon and word.
    assert label('connections.status.configured') in body
    assert label('connections.status.not_configured') in body
    assert '<a class="btn" href="/connections' in markup


def test_the_libraries_step_leads_to_the_library_choice(app, client, plex, conn):
    from lifecycle_helpers import seed_library

    seed_library(conn, '1', 'Movies')
    seed_library(conn, '2', 'TV Shows')
    body = page(client.get('/?lang=en'))
    assert label('setup.checklist.libraries.known', count=2) in body
    assert label('action.setup_libraries') in body
    assert '<a class="btn" href="/setup?step=libraries' in html(client.get('/?lang=en'))


@pytest.mark.parametrize('dry_run,expected', [
    (True, 'state.dry_run'),
    (False, 'state.real_deletion'),
])
def test_the_last_step_names_the_mode_and_leads_to_settings(app, client, plex, dry_run, expected):
    save_settings(replace(app.config['SETTINGS'], dry_run=dry_run))
    response = client.get('/?lang=en')
    body = page(response)
    assert label(expected) in body
    assert '<a class="btn" href="/settings' in html(response)


# -- S4: the header status ----------------------------------------------------


class _FixedDatetime(datetime):
    """A clock the header cannot disagree with."""

    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)
        return moment if tz is None else moment.astimezone(tz)


def test_the_header_shows_the_mode_the_last_and_the_next_analysis(app, client, plex, monkeypatch):
    from almali_library_curator.web import routes

    monkeypatch.setattr(routes, 'datetime', _FixedDatetime)
    snapshot(app, [displayed(1, 'Tenet', 10 * GB)])
    markup = html(client.get('/?lang=en'))
    body = page(client.get('/?lang=en'))
    assert label('scan.last') in body
    assert label('state.dry_run') in body
    next_line = re.search(r'<p class="next-scan">(.*?)</p>', markup, re.S)
    assert next_line, markup[:400]
    assert label('scan.next') in next_line.group(1)
    # The default daily analysis is 06:00, and the header says so.
    assert '06:00' in next_line.group(1)


def test_the_header_status_holds_on_the_other_screens(app, client, plex, monkeypatch):
    """The status lives in the shared shell, so every screen carries it."""
    from almali_library_curator.web import routes

    monkeypatch.setattr(routes, 'datetime', _FixedDatetime)
    snapshot(app, [displayed(1, 'Tenet', 10 * GB)])
    for path in ('/history', '/settings', '/connections', '/about'):
        body = page(client.get(path + '?lang=en'))
        assert label('scan.last') in body, path
        assert label('scan.next') in body, path
        assert label('state.dry_run') in body, path
    # A screen that must not read the settings still renders.
    anonymous = app.test_client()
    anonymous.get('/logout')
    assert anonymous.get('/login').status_code == 200


# -- S5: no fake zeros, no silent empty states --------------------------------


def test_the_summary_cards_show_no_fake_zero_before_the_first_analysis(app, client, plex):
    markup = html(client.get('/?lang=en'))
    body = page(client.get('/?lang=en'))
    # Candidates, Leaving soon and Protected are unknown, not zero: "—".
    assert markup.count(f'>{label("summary.pending_value")}<') == 3
    assert body.count(label('summary.pending')) == 3
    # Freed so far is a real zero: nothing has been deleted yet.
    assert '0 B' in body
    assert label('summary.freed') in body


def test_the_summary_cards_show_the_first_numbers_after_the_analysis(app, client, plex):
    snapshot(app, [displayed(1, 'Tenet', 142 * GB), displayed(2, 'Heat', 291 * GB)])
    body = page(client.get('/?lang=en'))
    assert '433 GB 2 items you could remove' in body
    assert label('summary.pending_value') not in body
    assert label('summary.pending') not in body


def test_the_scan_status_has_a_never_analysed_state(app, client):
    body = page(client.get('/setup?step=libraries&lang=en'))
    assert label('scan.status.never') in body
    assert label('scan.status.never_hint', time='06:00') in body


def test_the_candidates_empty_state_says_the_library_was_never_analysed(app, client, plex):
    body = page(client.get('/?lang=en'))
    assert label('empty.candidates_never_title') in body
    assert label('empty.candidates_never') in body
    assert label('empty.candidates_never_note', time='06:00') in body
    assert label('action.scan_now') in body
    assert label('empty.candidates') not in body


def test_the_candidates_empty_state_says_an_analysed_library_proposed_nothing(app, client, plex):
    snapshot(app, [])
    body = page(client.get('/?lang=en'))
    assert label('empty.candidates') in body
    assert label('empty.candidates_never_title') not in body
    assert label('empty.candidates_never') not in body


# -- S6: languages and the accessible shape of the checklist ------------------


@pytest.mark.parametrize('locale', SUPPORTED_LOCALES)
def test_no_first_run_key_is_ever_printed_raw(app, client, plex, locale):
    body = page(client.get(f'/?lang={locale}'))
    assert not [key for key in FIRST_RUN_KEYS if key in body]
    assert label('action.scan_now', locale) in body
    assert label('setup.state.pending', locale) in body


def test_every_first_run_string_exists_in_the_three_catalogs():
    catalogs = load_catalogs()
    for key in FIRST_RUN_KEYS:
        for locale in SUPPORTED_LOCALES:
            assert catalogs[locale].get(key, '').strip(), (locale, key)


def test_the_checklist_is_a_labelled_landmark_of_numbered_steps(app, client, plex):
    markup = html(client.get('/?lang=en'))
    opening = re.search(r'<section class="card setup-checklist" aria-labelledby="([^"]+)"', markup)
    assert opening, markup[:400]
    heading_id = opening.group(1)
    assert f'id="{heading_id}"' in markup
    # One ordered list, one number per step, and a progress element.
    assert '<ol class="setup-steps"' in markup
    assert markup.count('class="setup-step-number"') == 5
    assert f'aria-label="{label("setup.checklist.progress_label")}"' in markup
    assert '<progress' in markup
