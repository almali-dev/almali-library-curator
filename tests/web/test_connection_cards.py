"""S7 (T2): Connections is one card per service, state and form together.

The owner, verbatim: "El panell de connexions es molt confus. Ja que es veu que
sonarr, no esta configurat, però per configurar-ho has d'anar cap a baix." The
cards of design 06 keep the state, what the service adds, its actions and its
own form in the same card: Plex first and marked as the required connection,
then the optional ones, with one card open at a time and no script needed.
These tests read the pages the owner reads, through the Flask test client, and
assert the words of the catalogs rather than the markup of the artboard.
"""
from __future__ import annotations

import json
from dataclasses import replace
from html import unescape
from pathlib import Path
import re

import pytest

import almali_library_curator.web.views as views
from almali_library_curator import secret_store
from almali_library_curator.config import update_settings_file
from almali_library_curator.i18n import SUPPORTED_LOCALES, translate
from almali_library_curator.web import actions

ROOT = Path(__file__).resolve().parents[2] / 'almali_library_curator'
CATALOGS = {locale: json.loads((ROOT / f'i18n/{locale}.json').read_text())
            for locale in SUPPORTED_LOCALES}

# The order design 06 reads the cards in: the required connection leads.
CARD_ORDER = ('plex', 'tautulli', 'sonarr', 'radarr', 'tmdb', 'host')
# The cards that own a form; the host paths come from the configuration.
FORM_CARDS = ('plex', 'tautulli', 'sonarr', 'radarr', 'tmdb')
# Every string this unit adds (S6): one key, non-empty, in the three catalogs.
NEW_KEYS = (
    'connections.test_title',
    'connections.test_just_now',
    'connections.test_answered',
    'connections.test_explain',
    'connections.optional',
    'connections.card.configure',
    'connections.card.edit',
    'connections.plex.adds',
    'connections.host.media_root',
    'connections.host.unraid_root',
    'connections.host.note',
    'connections.status.unreachable_hint',
)


def label(key, locale='en', **params):
    """The catalog string of ``key``, formatted as the page formats it."""
    value = translate(key, locale)
    return value.format(**params) if params else value


def html(response):
    return response.get_data(as_text=True)


def words(markup):
    """The visible words of a fragment, entities decoded, single spaces."""
    text = unescape(re.sub(r'<[^>]+>', ' ', markup))
    return ' '.join(text.split())


def page(response):
    return words(html(response))


def card(markup, name):
    """The whole card of one service, from its <article> to its close."""
    match = re.search(rf'<article class="card svc[^"]*"[^>]*\bid="{name}"[^>]*>(.*?)</article>',
                      markup, re.S)
    assert match, f'no card for {name}'
    return match.group(0)


def details_of(block):
    """(opening tag, inner markup) of the card's disclosure, or None."""
    match = re.search(r'<details\b([^>]*)>(.*?)</details>', block, re.S)
    return match and (match.group(1), match.group(2))


def card_is_open(block):
    found = details_of(block)
    assert found, block[:300]
    return re.search(r'\bopen\b', found[0]) is not None


def open_cards(markup):
    """The cards whose form is open; a card with no form is never open."""
    return [name for name in FORM_CARDS
            if details_of(card(markup, name)) and card_is_open(card(markup, name))]


def healthy_probes():
    return {name: (lambda: True) for name in ('plex', 'sonarr', 'radarr', 'tautulli', 'tmdb', 'host')}


@pytest.fixture
def plex_environment(monkeypatch):
    """A Plex connection configured by the environment, as an install has."""
    monkeypatch.setenv('ALC_PLEX_BASE_URL', 'http://plex.example:32400')
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'plex-token')


# -- S7: one card per service, in the order of the artboard -------------------


def test_the_cards_lead_with_plex_and_mark_it_as_the_required_connection(client):
    markup = html(client.get('/connections?lang=en'))
    positions = [markup.index(f'id="{name}"') for name in CARD_ORDER]
    assert positions == sorted(positions), positions
    plex_card = card(markup, 'plex')
    assert label('connections.required') in words(plex_card)
    # The required badge belongs to Plex alone.
    assert page(client.get('/connections?lang=en')).count(label('connections.required')) == 1
    assert label('connections.optional') in words(card(markup, 'sonarr'))


def test_every_card_says_what_its_service_adds(client):
    markup = html(client.get('/connections?lang=en'))
    for name in ('tautulli', 'sonarr', 'radarr', 'tmdb', 'host'):
        assert label(f'setup.checklist.option.{name}') in words(card(markup, name)), name
    assert label('connections.plex.adds') in words(card(markup, 'plex'))


# -- S7: the state of every service, by icon and word -------------------------


def test_a_service_without_an_address_is_not_configured(client):
    markup = html(client.get('/connections?lang=en'))
    assert label('connections.status.not_configured') in words(card(markup, 'sonarr'))
    assert label('connections.status.not_configured') in words(card(markup, 'plex'))


def test_a_stored_key_reads_as_configured_without_contacting_the_service(app, client):
    directory = app.config['SETTINGS'].config_dir
    secret_store.store_secret(directory, 'sonarr_api_key', 'stored-sonarr-key')
    update_settings_file(directory, {'sonarr_base_url': 'http://sonarr.example:8989'})
    markup = html(client.get('/connections?lang=en'))
    block = card(markup, 'sonarr')
    assert label('connections.status.configured') in words(block)
    assert label('connections.status.unreachable') not in words(block)


def test_a_live_test_reports_a_connected_service(app, client, monkeypatch):
    monkeypatch.setattr(views, '_probes', lambda settings: healthy_probes())
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr.example:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'sonarr-key')
    markup = html(client.get('/connections?test=1&lang=en'))
    assert label('connections.status.connected') in words(card(markup, 'sonarr'))


def test_an_unreachable_service_says_its_error_in_its_own_card(app, client, monkeypatch):
    class ProbeExploded(Exception):
        pass

    def probes(settings):
        healthy = healthy_probes()
        def broken():
            raise ProbeExploded('never-show-this-secret')
        healthy['sonarr'] = broken
        return healthy

    monkeypatch.setattr(views, '_probes', probes)
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr.example:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'sonarr-key')
    response = client.get('/connections?test=1&lang=en')
    markup = html(response)
    block = card(markup, 'sonarr')
    assert label('connections.status.unreachable') in words(block)
    assert label('connections.status.unreachable_hint', service='Sonarr') in words(block)
    # The diagnostic stays in the log, never on the screen.
    assert 'never-show-this-secret' not in markup and 'ProbeExploded' not in markup


# -- S7: the form lives in its own card, folded, one card at a time -----------


@pytest.mark.parametrize('name', FORM_CARDS)
def test_the_form_of_a_service_sits_inside_its_card_and_is_closed_at_first(client, name):
    markup = html(client.get('/connections?lang=en'))
    block = card(markup, name)
    found = details_of(block)
    assert found, f'{name} has no disclosure'
    assert 'name="connections"' in found[0]
    assert not card_is_open(block)
    assert f'/connections/{name}' in found[1], f'{name} renders its form outside its card'


def test_only_one_card_can_be_open_at_a_time(client):
    markup = html(client.get('/connections?lang=en'))
    # One native exclusive group: no script, and the page still works without JS.
    assert markup.count('name="connections"') == len(FORM_CARDS)
    assert open_cards(markup) == []
    assert '<details' in markup and 'onclick=' not in markup


def test_the_host_paths_card_reports_the_paths_and_offers_no_form(client):
    block = card(html(client.get('/connections?lang=en')), 'host')
    assert details_of(block) is None
    assert label('connections.host.note') in words(block)
    assert label('connections.host.media_root') in words(block)


# -- S7: the server decides which card is open --------------------------------


def test_a_service_query_parameter_opens_that_card_only(client):
    markup = html(client.get('/connections?service=sonarr&lang=en'))
    assert open_cards(markup) == ['sonarr']


def test_an_unknown_service_query_parameter_opens_nothing(client):
    markup = html(client.get('/connections?service=emby&lang=en'))
    assert open_cards(markup) == []


def test_a_refused_save_renders_that_card_open_with_its_error(client):
    response = client.post('/connections/sonarr?lang=en',
                           data={'sonarr_base_url': 'sonarr:8989', 'sonarr_api_key': 'a-key-1234'})
    assert response.status_code == 400
    markup = html(response)
    block = card(markup, 'sonarr')
    assert card_is_open(block)
    assert label('web.form.service_address') in words(block)
    assert open_cards(markup) == ['sonarr']
    assert 'a-key-1234' not in markup


def test_a_refused_plex_save_renders_the_plex_card_open_with_its_error(client):
    response = client.post('/connections/plex?lang=en',
                           data={'plex_base_url': 'plex:32400', 'plex_token': 'a-token-1234'})
    assert response.status_code == 400
    markup = html(response)
    assert card_is_open(card(markup, 'plex'))
    assert label('web.form.plex_address') in words(card(markup, 'plex'))
    assert 'a-token-1234' not in markup


def test_a_successful_save_lands_on_the_cards_with_the_new_state_and_all_closed(app, client, monkeypatch):
    monkeypatch.setattr(actions, '_test_service', lambda *args: None)
    response = client.post('/connections/sonarr?lang=en',
                           data={'sonarr_base_url': 'http://sonarr.example:8989',
                                 'sonarr_api_key': 'a-key-1234'})
    assert response.status_code == 303
    markup = html(client.get(response.headers['Location']))
    block = card(markup, 'sonarr')
    assert not card_is_open(block)
    assert label('connections.status.configured') in words(block)
    assert open_cards(markup) == []


def test_a_pending_plex_sign_in_keeps_the_plex_card_open(app, client):
    state_path = app.config['SETTINGS'].config_dir / actions.SIGNIN_FILE_NAME
    state_path.write_text(json.dumps({'state': 'pending', 'code': 'ABCD',
                                      'client_identifier': 'test-client'}))
    markup = html(client.get('/connections?lang=en'))
    block = card(markup, 'plex')
    assert card_is_open(block)
    assert 'plex.tv' in block and 'code=ABCD' in block


def test_an_environment_managed_service_keeps_the_note_and_loses_the_form(client, monkeypatch):
    monkeypatch.setenv('ALC_SONARR_BASE_URL', 'http://sonarr.example:8989')
    monkeypatch.setenv('ALC_SONARR_API_KEY', 'sonarr-key')
    markup = html(client.get('/connections?lang=en'))
    block = card(markup, 'sonarr')
    assert details_of(block) is None
    assert label('connections.service.env_managed', service='Sonarr') in words(block)
    assert '/connections/sonarr' not in block


def test_a_refused_live_test_reopens_the_card_with_its_message(app, client, monkeypatch):
    monkeypatch.setattr(actions, '_test_service', lambda *args: 'web.form.service_unreachable')
    response = client.post('/connections/radarr?lang=en',
                           data={'radarr_base_url': 'http://radarr.example:7878',
                                 'radarr_api_key': 'a-key-1234'})
    assert response.status_code == 400
    markup = html(response)
    block = card(markup, 'radarr')
    assert card_is_open(block)
    assert label('web.form.service_unreachable', service='Radarr') in words(block)
    assert open_cards(markup) == ['radarr']
    assert 'a-key-1234' not in markup


def test_an_unreachable_host_path_is_reported_in_the_host_card(app, client, tmp_path, monkeypatch):
    from almali_library_curator.config import save_settings

    save_settings(replace(app.config['SETTINGS'], media_root=str(tmp_path / 'absent'), unraid_root=''))
    markup = html(client.get('/connections?test=1&lang=en'))
    block = card(markup, 'host')
    assert label('connections.status.unreachable') in words(block)
    assert label('connections.status.unreachable_hint', service=label('connections.host')) in words(block)


# -- S6/S7: one live test at the top, with the time it ran -------------------


def test_the_page_offers_one_live_test_and_names_the_time_it_ran(client, monkeypatch):
    monkeypatch.setattr(views, '_probes', lambda settings: healthy_probes())
    tested = page(client.get('/connections?test=1&lang=en'))
    assert label('connections.test_title') in tested
    assert re.search(r'Last live test: today \d\d:\d\d', tested), tested[:300]
    assert 'of 6 services answer' in tested
    assert label('connections.test_explain') in tested
    # A plain page load reads the configuration and promises nothing more.
    plain = page(client.get('/connections?lang=en'))
    assert label('connections.not_tested') in plain
    assert 'Last live test' not in plain


def test_an_anchor_to_a_card_is_addressable_and_opened_without_losing_the_page(client):
    markup = html(client.get('/connections?lang=en'))
    # Every card answers to the name of its service: /connections#sonarr.
    for name in CARD_ORDER:
        assert f'id="{name}"' in markup, name
    # The fragment is only visible to the browser, so a progressive script
    # opens that card; without it the card is still on screen and operable.
    assert re.search(r'<script src="/static/js/connections-disclosure\.js\?v=\w+" defer></script>',
                     markup)
    assert markup.count('<details') == markup.count('<summary'), 'every disclosure keeps its summary'


# -- the first-run checklist lands on the right card --------------------------


def test_the_checklist_opens_the_first_optional_connection_that_is_missing(client, plex_environment):
    markup = html(client.get('/?lang=en'))
    steps = re.findall(r'<li class="setup-step[^"]*">.*?</li>', markup, re.S)
    step = next((block for block in steps if label('action.setup_connections') in words(block)), None)
    assert step, 'no connections step in the checklist'
    action = re.search(r'<a class="btn" href="([^"]*)"', step)
    assert action and action.group(1).startswith('/connections?service='), step
    opened = action.group(1).split('service=', 1)[1].split('&', 1)[0]
    assert opened in CARD_ORDER
    # And the card that link names is the one the page opens.
    assert open_cards(html(client.get(action.group(1)))) == [opened]


# -- S6: languages and the same cards in the wizard ---------------------------


def test_the_wizard_connections_step_renders_the_same_cards(client):
    markup = html(client.get('/setup?step=connections&lang=en'))
    for name in ('tautulli', 'sonarr', 'radarr', 'tmdb'):
        block = card(markup, name)
        found = details_of(block)
        assert found and 'name="connections"' in found[0], name
        assert f'/connections/{name}' in found[1], name
    assert 'id="plex"' not in markup
    assert markup.count('name="connections"') == 4


def test_the_wizard_plex_step_keeps_the_plex_card_open(client):
    markup = html(client.get('/setup?step=plex&lang=en'))
    assert card_is_open(card(markup, 'plex'))


@pytest.mark.parametrize('locale', SUPPORTED_LOCALES)
def test_no_new_string_is_ever_printed_raw(client, locale):
    body = page(client.get(f'/connections?lang={locale}'))
    assert not [key for key in NEW_KEYS if key in body]
    assert label('connections.card.configure', locale) in body
    assert label('connections.test_title', locale) in body


def test_every_new_string_exists_in_the_three_catalogs():
    for key in NEW_KEYS:
        for locale in SUPPORTED_LOCALES:
            assert CATALOGS[locale].get(key, '').strip(), (locale, key)
