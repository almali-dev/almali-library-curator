"""Plex connection from the wizard and Connections (S12, T9a).

Every rejecting path proves that the settings file, both secrets files and
the database are exactly as they were before the request.
"""
from dataclasses import replace
import json
import logging
from pathlib import Path
import re
import socket
import stat

from cryptography.fernet import Fernet
from markupsafe import Markup
import pytest

from almali_library_curator import secret_store
from almali_library_curator.app import create_app
from almali_library_curator.config import load_settings, update_settings_file
from almali_library_curator.db import connect
from fake_plex_server import FakePlexServer

ROOT = Path(__file__).resolve().parents[2] / 'almali_library_curator'
CATALOGS = {locale: json.loads((ROOT / f'i18n/{locale}.json').read_text()) for locale in ('en', 'ca', 'es')}
EN = CATALOGS['en']
TOKEN = 'plex-secret-token-7f3c9ab12'
OTHER = 'other-plex-token-55d0e9zz'
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
def config_dir(app):
    return app.config['SETTINGS'].config_dir


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def snapshot(app):
    directory = app.config['SETTINGS'].config_dir
    files = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in FILES}
    db = connect(app.config['SETTINGS'].database_path)
    try:
        dump = list(db.iterdump())
    finally:
        db.close()
    return files, dump


def store(config_dir, token=TOKEN, address='http://stored-plex:32400'):
    secret_store.store_plex_token(config_dir, token)
    update_settings_file(config_dir, {'plex_base_url': address})


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return f'http://127.0.0.1:{port}'


def plex_form(html):
    match = re.search(r'<form method="post" action="(/connections/plex[^"]*)">(.*?)</form>', html, re.S)
    return match and (match.group(1).split('?')[0], match.group(2))


# -- rule 1: the form ---------------------------------------------------------

@pytest.mark.parametrize('path,origin', [('/connections', 'connections'), ('/setup?step=plex', 'setup')])
def test_the_form_asks_for_address_and_token_and_explains_the_token(client, path, origin):
    response = client.get(path)
    html = response.get_data(as_text=True)
    action, body = plex_form(html)
    assert action == '/connections/plex'
    assert 'name="csrf_token"' in body
    assert f'<input type="hidden" name="origin" value="{origin}"' in body
    assert 'name="plex_base_url"' in body
    password = re.search(r'<input[^>]*name="plex_token"[^>]*>', body).group(0)
    assert 'type="password"' in password and 'value=' not in password
    assert EN['connections.plex.token_help'] in text(response)


@pytest.mark.parametrize('route', ['/connections/plex', '/connections/plex/forget'])
def test_plex_posts_without_the_csrf_token_are_refused(settings, plex, route):
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    store(settings.config_dir)
    before = snapshot(app)
    response = app.test_client().post(route, data={'plex_base_url': plex.base_url, 'plex_token': OTHER})
    assert response.status_code == 400
    assert snapshot(app) == before and plex.requests == []


# -- rule 2: validation before anything is stored -----------------------------

INVALID_ADDRESSES = ['', 'plex:32400', 'ftp://plex:32400', 'http://', 'http://user:pw@plex:32400',
                     'http://user@plex:32400', 'http://plex:32400/?x=1', 'http://plex:32400/?',
                     'http://plex:32400/#top', 'http://plex:99999', 'javascript:alert(1)',
                     'http://plex host:32400', '//plex:32400']


@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('address', INVALID_ADDRESSES)
def test_an_invalid_address_changes_nothing(app, client, config_dir, plex, address, stored):
    if stored:
        store(config_dir)
    before = snapshot(app)
    response = client.post('/connections/plex', data={'plex_base_url': address, 'plex_token': TOKEN,
                                                     'origin': 'connections'})
    assert response.status_code == 400
    assert EN['web.form.plex_address'] in text(response)
    assert snapshot(app) == before and plex.requests == []
    assert TOKEN not in response.get_data(as_text=True)


@pytest.mark.parametrize('locale', ['en', 'ca', 'es'])
def test_an_empty_token_with_none_stored_changes_nothing(app, client, plex, locale):
    before = snapshot(app)
    response = client.post(f'/connections/plex?lang={locale}',
                           data={'plex_base_url': plex.base_url, 'plex_token': '  ', 'origin': 'setup'})
    assert response.status_code == 400
    assert CATALOGS[locale]['web.form.plex_token_required'] in text(response)
    # The wizard re-renders its Plex step.
    assert CATALOGS[locale]['setup.plex_intro'] in text(response)
    assert snapshot(app) == before and plex.requests == []


def test_a_token_with_spaces_is_refused_before_contacting_plex(app, client, plex):
    before = snapshot(app)
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url,
                                                     'plex_token': 'two words'})
    assert response.status_code == 400
    assert EN['web.form.plex_token_invalid'] in text(response)
    assert snapshot(app) == before and plex.requests == []


# -- rule 3: test before save -------------------------------------------------

def _failing(plex, failure):
    if failure == 'unreachable':
        return closed_port_url(), TOKEN
    if failure == 'refused':
        return plex.base_url, 'a-token-plex-refuses-000'
    plex.dataset.identity = {'friendlyName': 'Not a Plex server'}
    return plex.base_url, TOKEN


@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('failure,key', [('unreachable', 'web.form.plex_unreachable'),
                                         ('refused', 'web.form.plex_token_refused'),
                                         ('identity', 'web.form.plex_not_a_server')])
def test_a_failed_plex_test_names_the_failure_and_stores_nothing(app, client, config_dir, plex,
                                                                failure, key, stored):
    if stored:
        store(config_dir, token=OTHER)
    address, token = _failing(plex, failure)
    before = snapshot(app)
    response = client.post('/connections/plex?lang=ca', data={'plex_base_url': address, 'plex_token': token,
                                                             'origin': 'connections'})
    assert response.status_code == 400
    body = text(response)
    assert CATALOGS['ca'][key] in body
    assert all(CATALOGS['ca'][other] not in body for _, other in
               [('', 'web.form.plex_unreachable'), ('', 'web.form.plex_token_refused'),
                ('', 'web.form.plex_not_a_server')] if other != key)
    assert snapshot(app) == before
    assert token not in response.get_data(as_text=True) and OTHER not in response.get_data(as_text=True)


def test_a_plex_server_error_is_reported_as_unreachable(app, client, plex):
    plex.dataset.fail_always('/', 503)
    before = snapshot(app)
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN})
    assert response.status_code == 400 and EN['web.form.plex_unreachable'] in text(response)
    assert snapshot(app) == before


def test_a_redirecting_address_is_not_a_plex_server_and_the_token_is_not_followed(app, client, plex):
    plex.dataset.redirects['/'] = 'http://elsewhere.invalid/'
    before = snapshot(app)
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN})
    assert response.status_code == 400 and EN['web.form.plex_not_a_server'] in text(response)
    assert snapshot(app) == before


# -- rule 4: storage ----------------------------------------------------------

@pytest.mark.parametrize('origin,location', [('connections', '/connections'), ('setup', '/setup')])
def test_a_tested_connection_is_saved_with_the_token_encrypted(app, client, config_dir, plex, origin, location):
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url + '/', 'plex_token': TOKEN,
                                                     'origin': origin})
    assert response.status_code == 303
    assert response.headers['Location'].split('?')[0] == location
    if origin == 'setup':
        assert 'step=plex' in response.headers['Location']
    assert plex.requests_for('/')[0]['headers']['X-Plex-Token'] == TOKEN
    saved = (config_dir / 'settings.json').read_text()
    assert json.loads(saved)['plex_base_url'] == plex.base_url
    assert TOKEN not in saved and 'plex_token' not in json.loads(saved)
    for name in ('secrets.json', 'secrets.key'):
        assert stat.S_IMODE((config_dir / name).stat().st_mode) == 0o600
        assert TOKEN.encode() not in (config_dir / name).read_bytes()
    loaded = load_settings(config_dir, env={})
    assert loaded.plex_base_url == plex.base_url and loaded.plex_token == TOKEN
    followed = client.get(response.headers['Location'])
    assert EN['web.flash.plex_saved'] in text(followed)
    assert TOKEN not in followed.get_data(as_text=True) and TOKEN not in response.get_data(as_text=True)
    with client.session_transaction() as session:
        assert TOKEN not in repr(dict(session))


def test_secrets_files_are_never_served(app, client, config_dir, plex):
    store(config_dir)
    for path in ('/secrets.json', '/secrets.key', '/static/secrets.json', '/static/../secrets.key',
                 '/config/secrets.json'):
        response = client.get(path)
        assert response.status_code == 404
        assert TOKEN not in response.get_data(as_text=True)


# -- rule 5: never returned in full -------------------------------------------

@pytest.mark.parametrize('path', ['/connections', '/setup?step=plex'])
def test_a_stored_token_shows_only_its_last_four_characters(client, config_dir, path):
    store(config_dir)
    response = client.get(path)
    html = response.get_data(as_text=True)
    assert '••••' + TOKEN[-4:] in text(response)
    assert TOKEN not in html and TOKEN[:-4] not in html
    password = re.search(r'<input[^>]*name="plex_token"[^>]*>', html).group(0)
    assert 'value=' not in password


# -- rule 6: keeping the token ------------------------------------------------

def test_an_empty_token_keeps_the_stored_token_and_tests_the_new_address(app, client, config_dir, plex):
    store(config_dir, address='http://old-plex:32400')
    secrets_before = {name: (config_dir / name).read_bytes() for name in ('secrets.json', 'secrets.key')}
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': ''})
    assert response.status_code == 303
    assert plex.requests_for('/')[0]['headers']['X-Plex-Token'] == TOKEN
    assert {name: (config_dir / name).read_bytes() for name in secrets_before} == secrets_before
    loaded = load_settings(config_dir, env={})
    assert loaded.plex_base_url == plex.base_url and loaded.plex_token == TOKEN


def test_a_new_token_replaces_the_stored_one(app, client, config_dir, plex):
    store(config_dir, token=OTHER)
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN})
    assert response.status_code == 303
    assert load_settings(config_dir, env={}).plex_token == TOKEN


# -- rule 7: forget -----------------------------------------------------------

@pytest.mark.parametrize('origin,location', [('connections', '/connections'), ('setup', '/setup')])
def test_forget_deletes_the_stored_token_and_plex_is_not_configured(app, client, config_dir, origin, location):
    store(config_dir)
    page = client.get('/connections').get_data(as_text=True)
    assert 'action="/connections/plex/forget' in page and EN['connections.plex.forget'] in page
    response = client.post('/connections/plex/forget', data={'origin': origin})
    assert response.status_code == 303 and response.headers['Location'].split('?')[0] == location
    assert load_settings(config_dir, env={}).plex_token == ''
    body = text(client.get('/connections'))
    assert 'Plex Not configured' in body
    assert EN['web.flash.plex_forgotten'] in body
    assert '••••' not in body and EN['connections.plex.token_unreadable'] not in body


# -- rule 8: environment wins -------------------------------------------------

@pytest.mark.parametrize('variable', ['ALC_PLEX_BASE_URL', 'ALC_PLEX_TOKEN'])
def test_the_environment_replaces_the_form_and_refuses_posts(app, client, config_dir, plex, monkeypatch, variable):
    store(config_dir, token=OTHER)
    monkeypatch.setenv(variable, 'http://env-plex:32400' if variable.endswith('URL') else 'env-token-0000')
    for path in ('/connections', '/setup?step=plex'):
        page = client.get(path)
        assert EN['connections.plex.env_managed'] in text(page)
        assert plex_form(page.get_data(as_text=True)) is None
        assert 'name="plex_token"' not in page.get_data(as_text=True)
    before = snapshot(app)
    for route, data in (('/connections/plex', {'plex_base_url': plex.base_url, 'plex_token': TOKEN}),
                        ('/connections/plex/forget', {})):
        response = client.post(route + '?lang=es', data=data)
        assert response.status_code == 409
        assert CATALOGS['es']['connections.plex.env_managed'] in text(response)
        assert snapshot(app) == before and plex.requests == []


# -- rule 9: unreadable secrets fail safe -------------------------------------

def _damage(config_dir, damage):
    if damage == 'corrupt_store':
        (config_dir / 'secrets.json').write_text('{"plex_token": 42')
    elif damage == 'missing_key':
        (config_dir / 'secrets.key').unlink()
    else:
        (config_dir / 'secrets.key').write_bytes(Fernet.generate_key())


@pytest.mark.parametrize('damage', ['corrupt_store', 'missing_key', 'wrong_key'])
def test_unreadable_secrets_show_not_configured_and_ask_for_the_token_again(
        settings, client, config_dir, plex, monkeypatch, caplog, damage):
    store(config_dir, address=plex.base_url)
    _damage(config_dir, damage)
    caplog.set_level(logging.INFO)
    # A fresh start through the environment never raises.
    monkeypatch.setenv('ALC_CONFIG_DIR', str(config_dir))
    monkeypatch.setenv('ALC_SCHEDULER_ENABLED', 'false')
    create_app()
    for path in ('/connections', '/setup?step=plex'):
        body = text(client.get(path))
        assert 'Plex Not configured' in body
        assert EN['connections.plex.token_unreadable'] in body
    assert any(record.getMessage() == 'secret_store_unreadable' for record in caplog.records)
    assert TOKEN not in caplog.text
    # Entering the token again repairs the store.
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN})
    assert response.status_code == 303
    assert load_settings(config_dir, env={}).plex_token == TOKEN
    assert EN['connections.plex.token_unreadable'] not in text(client.get('/connections'))


def test_an_empty_token_with_an_unreadable_store_is_required(app, client, config_dir, plex):
    store(config_dir, address=plex.base_url)
    _damage(config_dir, 'wrong_key')
    before = snapshot(app)
    response = client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': ''})
    assert response.status_code == 400 and EN['web.form.plex_token_required'] in text(response)
    assert snapshot(app) == before and plex.requests == []


# -- rule 4: the token never reaches a log line --------------------------------

def test_no_flow_logs_the_token(app, client, config_dir, plex, caplog):
    from almali_library_curator.logging_config import JsonFormatter
    caplog.set_level(logging.DEBUG)
    client.post('/connections/plex', data={'plex_base_url': closed_port_url(), 'plex_token': TOKEN})
    client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': 'refused-token-1234'})
    client.post('/connections/plex', data={'plex_base_url': plex.base_url, 'plex_token': TOKEN})
    client.get('/connections?test=1')
    client.post('/connections/plex/forget', data={})
    formatter = JsonFormatter()
    rendered = '\n'.join(formatter.format(record) for record in caplog.records)
    assert caplog.records
    for secret in (TOKEN, 'refused-token-1234'):
        assert secret not in caplog.text and secret not in rendered


# -- regression: plain page loads never contact a service ---------------------

def test_plain_connections_and_setup_loads_contact_no_service(client, config_dir, plex, monkeypatch):
    import almali_library_curator.web.views as views
    store(config_dir, address=plex.base_url)
    monkeypatch.setattr(views, '_probes', lambda settings: pytest.fail('probed on a plain page load'))
    for path in ('/connections', '/setup', '/setup?step=plex', '/setup?step=libraries',
                 '/setup?step=connections'):
        assert client.get(path).status_code == 200
    assert plex.requests == []
