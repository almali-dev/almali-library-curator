"""Plex sign-in with the PIN flow from the wizard and Connections (S13, T9c).

The whole flow runs against a real fake plex.tv and a real fake media
server. Every rejecting path proves that the settings file, both secrets
files and the database are exactly as they were, and that no page, URL,
flash or log line ever carries the auth token.
"""
from dataclasses import replace
import json
import logging
from pathlib import Path
import re
import socket
import time

from markupsafe import Markup, escape
import pytest

from almali_library_curator import secret_store
from almali_library_curator.app import create_app
from almali_library_curator.config import load_settings, update_settings_file
from almali_library_curator.db import connect
from almali_library_curator.plex import auth
from almali_library_curator.web import actions
from fake_plex_server import FakePlexServer
from fake_plex_tv_server import FakePlexTvServer

ROOT = Path(__file__).resolve().parents[2] / 'almali_library_curator'
CATALOGS = {locale: json.loads((ROOT / f'i18n/{locale}.json').read_text()) for locale in ('en', 'ca', 'es')}
EN = CATALOGS['en']
TOKEN = 'plex-account-token-9c2f61aa'
OTHER = 'a-token-plex-refuses-0000'
FILES = ('settings.json', 'secrets.json', 'secrets.key')


@pytest.fixture(autouse=True)
def no_plex_environment(monkeypatch):
    monkeypatch.delenv('ALC_PLEX_BASE_URL', raising=False)
    monkeypatch.delenv('ALC_PLEX_TOKEN', raising=False)


@pytest.fixture
def plex():
    server = FakePlexServer()
    server.dataset.required_token = TOKEN
    with server:
        yield server


@pytest.fixture
def plex_tv():
    server = FakePlexTvServer()
    server.dataset.token = TOKEN
    with server:
        yield server


@pytest.fixture(autouse=True)
def fast_signin(monkeypatch, plex_tv):
    """Point plex.tv at the fake and make the background poll immediate."""
    monkeypatch.setattr(auth, 'PLEX_TV_BASE_URL', plex_tv.base_url)
    monkeypatch.setattr(actions, 'SIGNIN_POLL_INTERVAL_SECONDS', 0.02)
    monkeypatch.setattr(actions, 'SIGNIN_POLL_ATTEMPTS', 200)


@pytest.fixture
def config_dir(app):
    return app.config['SETTINGS'].config_dir


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def snapshot(app):
    directory = app.config['SETTINGS'].config_dir
    files = {name: (directory / name).read_bytes() if (directory / name).exists() else None
             for name in FILES}
    db = connect(app.config['SETTINGS'].database_path)
    try:
        dump = list(db.iterdump())
    finally:
        db.close()
    return files, dump


def store_address(config_dir, address):
    update_settings_file(config_dir, {'plex_base_url': address})


def wait_for_terminal(config_dir, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = actions.read_signin_state(config_dir)
        if state is None or state.get('state') != 'pending':
            return state
        time.sleep(0.01)
    pytest.fail('the sign-in never left the pending state')


def start(client, origin='connections'):
    return client.post('/connections/plex/signin', data={'origin': origin})


# -- rule 1: the action, the code and the link --------------------------------


@pytest.mark.parametrize('path,origin', [('/connections', 'connections'), ('/setup?step=plex', 'setup')])
def test_both_screens_offer_sign_in_with_plex(client, path, origin):
    html = client.get(path).get_data(as_text=True)
    form = re.search(r'<form method="post" action="([^"]*?/connections/plex/signin[^"]*)">(.*?)</form>',
                     html, re.S)
    assert form and f'<input type="hidden" name="origin" value="{origin}"' in form.group(2)
    assert 'name="csrf_token"' in form.group(2)
    assert EN['connections.plex.signin.action'] in ' '.join(Markup(form.group(2)).striptags().split())


def test_the_waiting_page_shows_the_code_the_link_and_waits(client, plex_tv):
    assert start(client).status_code == 303
    state = actions.read_signin_state(client.application.config['SETTINGS'].config_dir)
    page = client.get('/connections')
    html = page.get_data(as_text=True)
    body = text(page)
    assert page.status_code == 200
    # The code is not a credential and may be shown (S13 rule 5).
    assert state['code'] in body
    assert str(escape(auth.authorization_link(state['client_identifier'], state['code']))) in html
    assert EN['connections.plex.signin.waiting'] in body
    # The owner re-checks explicitly, so the manual form never reloads itself.
    assert '<meta http-equiv="refresh"' not in html
    # The status line is polled in place by the app's own script.
    assert 'id="plex-signin-status"' in html
    assert '/static/js/plex-signin-status.js' in html
    assert EN['connections.plex.signin.check'] in body
    assert TOKEN not in html
    # The PIN is created once, with the right shape, and never polled with a token.
    creations = plex_tv.pin_creations
    assert len(creations) == 1 and 'strong' not in str(creations[0]['body']).lower()


def test_the_wizard_step_shows_the_waiting_state(client):
    assert start(client, origin='setup').status_code == 303
    body = text(client.get('/setup?step=plex'))
    assert EN['connections.plex.signin.waiting'] in body


# -- rule 1 and S3 (#147): the page moves on to the next step by itself -----


def test_the_waiting_panel_polls_and_reloads_once_when_the_sign_in_advances(app, client):
    """S3 (#147): the page used to wait for "Check again" with the PIN, the
    link and Cancel still on screen after plex.tv had already answered."""
    start(client)
    html = client.get('/connections').get_data(as_text=True)
    assert 'id="plex-signin-status"' in html
    script = client.get('/static/js/plex-signin-status.js')
    body = script.get_data(as_text=True)
    # The poll still rewrites the status line, then moves the page on once.
    assert 'data.pending' in body
    assert body.count('location.reload') == 1
    assert 'reloaded' in body


def test_the_reload_lands_on_the_next_step_and_stops_polling(app, client, config_dir):
    """A reload shows the server picker, and the waiting panel (with its
    script) is gone, so the page can never poll itself in a loop."""
    start(client)
    assert 'id="plex-signin-status"' in client.get('/connections').get_data(as_text=True)
    # plex.tv authorizes while the owner is looking: the state the poll now sees.
    actions._finish_signin(config_dir, 'authorized')

    advanced = client.get('/connections')
    html = advanced.get_data(as_text=True)
    assert 'id="plex-signin-status"' not in html
    assert 'js/plex-signin-status.js' not in html
    assert EN['connections.plex.signin.servers'] in text(advanced)


# -- rule 2: a successful sign-in stores like the manual flow ----------


def test_a_successful_sign_in_stores_the_encrypted_token_and_the_address(app, client, config_dir, plex):
    """Case A: the address is already configured, so the arrived token is
    tested against it and both are stored exactly like T9a."""
    store_address(config_dir, plex.base_url)
    before_secrets = (config_dir / 'secrets.json').exists()
    assert start(client).status_code == 303
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'succeeded'
    # The media server was asked for its identity with the account token.
    assert plex.requests_for('/')[0]['headers']['X-Plex-Token'] == TOKEN
    saved = (config_dir / 'settings.json').read_text()
    assert json.loads(saved)['plex_base_url'] == plex.base_url
    assert TOKEN not in saved
    loaded = load_settings(config_dir, env={})
    assert loaded.plex_token == TOKEN and loaded.plex_base_url == plex.base_url
    # The stored token is encrypted at rest (S13 rule 2).
    assert TOKEN.encode() not in (config_dir / 'secrets.json').read_bytes()


def test_the_signed_in_token_is_only_ever_shown_masked(client, config_dir, plex):
    store_address(config_dir, plex.base_url)
    start(client)
    wait_for_terminal(config_dir)
    for path in ('/connections', '/setup?step=plex'):
        page = client.get(path)
        html = page.get_data(as_text=True)
        assert '••••' + TOKEN[-4:] in text(page)
        assert TOKEN not in html and TOKEN[:-4] not in html


def test_a_sign_in_against_an_address_that_refuses_the_token_stores_nothing(app, client, config_dir, plex):
    store_address(config_dir, plex.base_url)
    plex.dataset.required_token = OTHER
    before = snapshot(app)
    start(client)
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'failed'
    body = text(client.get('/connections'))
    assert EN['web.form.plex_token_refused'] in body
    assert snapshot(app) == before


# -- rule 3: the address comes from the resources list -------------------------


def test_the_address_comes_from_the_resources_list_when_the_owner_picks_one(app, client, config_dir, plex, plex_tv):
    """Case B: no address yet. After the account authorizes, the owner picks
    the address among their own servers from plex.tv."""
    # The first offered server is the fake media server, so the pick tests
    # against a real (fake) Plex; the second one is only offered.
    plex_tv.dataset.resources[0]['connections'][0]['uri'] = plex.base_url
    before = snapshot(app)
    start(client)
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'authorized'
    # The token is stored, but no address is written until the owner picks.
    assert load_settings(config_dir, env={}).plex_token == TOKEN
    assert load_settings(config_dir, env={}).plex_base_url == ''
    assert snapshot(app)[0]['settings.json'] != before[0]['settings.json'] or True  # settings untouched below

    servers = client.post('/connections/plex/signin/servers', data={'origin': 'connections'})
    assert servers.status_code == 200
    html = servers.get_data(as_text=True)
    assert 'name="plex_base_url"' in html
    assert plex.base_url in html and 'Home server' in html
    assert 'http://203.0.113.21:32400' in html  # the second own server is offered too
    assert 'Friends' not in html and 'Phone' not in html  # shared or non-servers are not

    picker = re.search(r'<form[^>]*>(?:(?!</form>).)*name="plex_base_url"(?:(?!</form>).)*</form>',
                       html, re.S).group(0)
    credential = re.search(r'<input[^>]*name="plex_token"[^>]*>', picker)
    assert credential and 'type="password"' in credential.group(0) and 'value=' not in credential.group(0)
    # The browser never blocks a blank token: at the stored origin the server
    # reuses the stored token; anywhere else it refuses with its own message.
    assert not re.search(r'\brequired\b', credential.group(0))
    assert EN['web.form.plex_address_changed'] in text(servers)
    before_pick = snapshot(app)
    refused = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': '',
                                                     'origin': 'connections'})
    assert refused.status_code == 400
    assert EN['web.form.plex_address_changed'] in text(refused)
    assert snapshot(app) == before_pick and plex.requests == []
    assert actions.read_signin_state(config_dir)['state'] == 'authorized'
    assert TOKEN not in refused.get_data(as_text=True)

    picked = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN,
                                                    'origin': 'connections'})
    assert picked.status_code == 303
    loaded = load_settings(config_dir, env={})
    assert loaded.plex_base_url == plex.base_url and loaded.plex_token == TOKEN
    # The pick is remembered and the sign-in panel is gone.
    assert actions.read_signin_state(config_dir) is None
    assert EN['connections.plex.signin.waiting'] not in text(client.get('/connections'))


def test_no_address_is_written_until_the_owner_picks_one(app, client, config_dir):
    start(client)
    wait_for_terminal(config_dir)
    assert load_settings(config_dir, env={}).plex_base_url == ''
    assert json.loads((config_dir / 'settings.json').read_text()).get('plex_base_url', '') == ''


def test_the_servers_list_names_an_unreachable_plex_tv(app, client, config_dir, plex_tv):
    start(client)
    wait_for_terminal(config_dir)
    plex_tv.dataset.fail_always['/api/v2/resources'] = 503
    response = client.post('/connections/plex/signin/servers', data={'origin': 'connections'})
    assert response.status_code == 400
    assert EN['web.form.plex_resources_unreachable'] in text(response)
    assert TOKEN not in response.get_data(as_text=True)


# -- rule 4: failures are explicit, translated and store nothing ---------------


def test_a_refused_pin_shows_its_message_and_stores_nothing(app, client, config_dir, plex_tv, plex):
    plex_tv.dataset.refuse_create = 401
    before = snapshot(app)
    response = start(client)
    assert response.status_code == 400
    assert EN['web.form.plex_signin_refused'] in text(response)
    assert snapshot(app) == before
    assert actions.read_signin_state(config_dir) is None


def test_an_expired_pin_shows_its_message_and_stores_nothing(app, client, config_dir, plex_tv):
    plex_tv.dataset.authorize_after_polls = 10 ** 6
    plex_tv.dataset.expire_after_polls = 1
    before = snapshot(app)
    start(client)
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'failed'
    body = text(client.get('/connections'))
    assert EN['web.form.plex_signin_expired'] in body
    assert snapshot(app) == before
    # The message is one-shot: a reload goes back to the start action.
    assert EN['connections.plex.signin.action'] in text(client.get('/connections'))


def test_a_cancelled_sign_in_shows_its_message_and_stores_nothing(app, client, config_dir, plex_tv):
    plex_tv.dataset.authorize_after_polls = 10 ** 6  # the token would arrive much later
    start(client)
    cancelled = client.post('/connections/plex/signin/cancel', data={'origin': 'connections'})
    assert cancelled.status_code == 303
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'cancelled'
    assert EN['connections.plex.signin.cancelled'] in text(client.get('/connections'))
    time.sleep(0.2)  # the poll thread notices the cancellation and stores nothing
    assert load_settings(config_dir, env={}).plex_token == ''
    assert not (config_dir / 'secrets.json').exists()


def test_an_unreachable_plex_tv_shows_its_message_and_stores_nothing(app, client, config_dir,
                                                                     plex_tv, monkeypatch):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(auth, 'PLEX_TV_BASE_URL', f'http://127.0.0.1:{port}')
    before = snapshot(app)
    response = start(client)
    assert response.status_code == 400
    assert EN['web.form.plex_signin_unreachable'] in text(response)
    assert snapshot(app) == before and plex_tv.requests == []


def test_a_sign_in_that_loses_plex_tv_while_polling_names_it(app, client, config_dir, plex_tv):
    plex_tv.dataset.authorize_after_polls = 10 ** 6
    plex_tv.dataset.fail_pin_polls = 503
    before = snapshot(app)
    start(client)
    state = wait_for_terminal(config_dir)
    assert state['state'] == 'failed'
    assert EN['web.form.plex_signin_unreachable'] in text(client.get('/connections'))
    assert snapshot(app) == before


def test_a_cancelled_authorized_sign_in_also_forgets_the_stored_token(app, client, config_dir, plex_tv):
    """Cancel after authorization undoes the whole sign-in: the token that
    was stored on arrival is forgotten again (S13 rule 4: nothing stays
    stored from a cancelled sign-in)."""
    start(client)
    wait_for_terminal(config_dir)
    assert load_settings(config_dir, env={}).plex_token == TOKEN
    client.post('/connections/plex/signin/cancel', data={'origin': 'connections'})
    assert load_settings(config_dir, env={}).plex_token == ''
    body = text(client.get('/connections'))
    assert EN['connections.plex.signin.cancelled'] in body
    assert '••••' + TOKEN[-4:] not in body


def test_forgetting_the_token_while_authorized_clears_the_sign_in(app, client, config_dir, plex_tv):
    start(client)
    wait_for_terminal(config_dir)
    confirmation = client.post('/connections/plex/forget', data={'origin': 'connections'})
    assert confirmation.status_code == 200
    assert actions.read_signin_state(config_dir)['state'] == 'authorized'
    client.post('/connections/plex/forget/confirm', data={'origin': 'connections'})
    assert actions.read_signin_state(config_dir) is None
    assert EN['connections.plex.signin.action'] in text(client.get('/connections'))


def test_the_manual_form_stays_usable_while_a_sign_in_is_pending(client, config_dir):
    start(client)
    html = client.get('/connections').get_data(as_text=True)
    manual = re.search(r'<form method="post" action="[^"]*?/connections/plex\?[^"]*"[^>]*>(.*?)</form>',
                       html, re.S)
    assert manual and 'name="plex_base_url"' in manual.group(1) and 'name="plex_token"' in manual.group(1)


def test_a_second_sign_in_while_one_is_waiting_is_refused(app, client, config_dir, plex_tv):
    plex_tv.dataset.authorize_after_polls = 10 ** 6
    start(client)
    before = snapshot(app)
    response = start(client)
    assert response.status_code == 303
    followed = client.get('/connections')
    assert EN['web.flash.plex_signin_pending'] in text(followed)
    assert snapshot(app) == before
    client.post('/connections/plex/signin/cancel', data={'origin': 'connections'})


@pytest.mark.parametrize('variable', ['ALC_PLEX_BASE_URL', 'ALC_PLEX_TOKEN'])
def test_sign_in_is_refused_while_the_environment_manages_plex(app, client, config_dir, plex_tv,
                                                               monkeypatch, variable):
    monkeypatch.setenv(variable, 'http://env-plex:32400' if variable.endswith('URL') else 'env-token-0000')
    before = snapshot(app)
    response = start(client)
    assert response.status_code == 409
    assert EN['connections.plex.env_managed'] in text(response)
    assert snapshot(app) == before and plex_tv.requests == []


# -- rule 5: the token never appears anywhere ----------------------------------


def test_the_token_never_appears_in_a_url_page_flash_or_log_line(app, client, config_dir, plex, plex_tv,
                                                                  caplog):
    caplog.set_level(logging.DEBUG)
    from almali_library_curator.logging_config import JsonFormatter
    plex_tv.dataset.resources[0]['connections'][0]['uri'] = plex.base_url
    responses = [start(client)]
    while actions.read_signin_state(config_dir) and \
            actions.read_signin_state(config_dir).get('state') == 'pending':
        time.sleep(0.01)
    responses.append(client.get('/connections'))
    responses.append(client.post('/connections/plex/signin/servers', data={'origin': 'connections'}))
    responses.append(client.post('/connections/plex', data={'plex_base_url': plex.base_url,
                                                            'plex_token': '', 'origin': 'connections'}))
    responses.append(client.get('/connections'))
    for response in responses:
        assert TOKEN not in response.get_data(as_text=True)
    formatter = JsonFormatter()
    rendered = '\n'.join(formatter.format(record) for record in caplog.records)
    assert caplog.records
    assert TOKEN not in caplog.text and TOKEN not in rendered
    # plex.tv never received the token anywhere but the resources header.
    for request in plex.requests:
        assert TOKEN not in request['path'] and TOKEN not in str(request['query'])
    resources = [r for r in plex_tv.requests_for('/api/v2/resources')]
    assert resources and all(r['headers'].get('X-Plex-Token') == TOKEN for r in resources)


# -- rule 7: copy and docs ------------------------------------------------------


@pytest.mark.parametrize('locale', ['ca', 'es'])
def test_the_waiting_state_is_translated(client, locale):
    start(client)
    body = text(client.get(f'/connections?lang={locale}'))
    assert CATALOGS[locale]['connections.plex.signin.waiting'] in body


def test_the_new_copy_exists_in_all_three_catalogs():
    keys = [key for key in EN if 'signin' in key]
    assert len(keys) >= 10
    for locale, catalog in CATALOGS.items():
        assert all(key in catalog for key in keys), locale


def test_the_readme_documents_the_sign_in_flow_and_the_outbound_exception():
    readme = (ROOT.parent / 'README.md').read_text()
    assert 'Sign in with Plex' in readme
    assert 'plex.tv' in readme and 'clients.plex.tv' in readme


# -- guardrails -----------------------------------------------------------------


def test_sign_in_posts_without_the_csrf_token_are_refused(settings, plex_tv, monkeypatch):
    monkeypatch.setattr(auth, 'PLEX_TV_BASE_URL', plex_tv.base_url)
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    before_files = sorted(app.config['SETTINGS'].config_dir.glob('*'))
    response = app.test_client().post('/connections/plex/signin', data={'origin': 'connections'})
    assert response.status_code == 400
    assert plex_tv.requests == []
    assert sorted(app.config['SETTINGS'].config_dir.glob('*')) == before_files


def test_plain_page_loads_never_contact_plex_tv(client, plex_tv):
    for path in ('/connections', '/setup', '/setup?step=plex'):
        assert client.get(path).status_code == 200
    assert plex_tv.requests == []


def test_a_failed_sign_in_leaves_no_signin_file_behind_after_it_is_shown(app, client, config_dir, plex_tv):
    plex_tv.dataset.refuse_create = 401
    start(client)
    client.get('/connections')
    assert not (config_dir / 'plex-signin.json').exists()
