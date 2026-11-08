"""Service keys entered in the app (S20, T11b).

Sonarr, Radarr, Tautulli and TMDB keys are entered on Connections and in the
wizard, tested against the service, then stored encrypted in the same Fernet
store as the Plex token. Every rejecting path proves that the settings file,
both secrets files and the database are exactly as they were.
"""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat

from markupsafe import Markup
import pytest

from almali_library_curator import secret_store
from almali_library_curator.app import create_app
from almali_library_curator.config import load_settings, update_settings_file
from almali_library_curator.db import connect
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer
from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie

ROOT = Path(__file__).resolve().parents[2] / 'almali_library_curator'
CATALOGS = {locale: json.loads((ROOT / f'i18n/{locale}.json').read_text()) for locale in ('en', 'ca', 'es')}
EN = CATALOGS['en']
KEY = 'service-secret-key-7f3c9ab12'
OTHER = 'other-service-key-55d0e9zz'
PLEX_TOKEN = 'plex-secret-token-7f3c9ab12'
FILES = ('settings.json', 'secrets.json', 'secrets.key')


@dataclass(frozen=True)
class Service:
    name: str
    brand: str
    key_field: str
    url_field: str | None
    env: tuple[str, ...]


SERVICES = {
    'sonarr': Service('sonarr', 'Sonarr', 'sonarr_api_key', 'sonarr_base_url',
                      ('ALC_SONARR_BASE_URL', 'ALC_SONARR_API_KEY')),
    'radarr': Service('radarr', 'Radarr', 'radarr_api_key', 'radarr_base_url',
                      ('ALC_RADARR_BASE_URL', 'ALC_RADARR_API_KEY')),
    'tautulli': Service('tautulli', 'Tautulli', 'tautulli_api_key', 'tautulli_base_url',
                        ('ALC_TAUTULLI_BASE_URL', 'ALC_TAUTULLI_API_KEY')),
    'tmdb': Service('tmdb', 'TMDB', 'tmdb_api_token', None, ('ALC_TMDB_API_TOKEN',)),
}
NAMES = list(SERVICES)
ALL_ENV = [name for service in SERVICES.values() for name in service.env] + [
    'ALC_TMDB_BASE_URL', 'ALC_PLEX_BASE_URL', 'ALC_PLEX_TOKEN']


@pytest.fixture(autouse=True)
def no_service_environment(monkeypatch):
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)


class FakeService:
    """One running fake server that accepts ``key`` and records requests."""

    def __init__(self, name, key=KEY):
        self.name = name
        if name == 'sonarr':
            dataset = FakeSonarrDataset()
            dataset.api_key = key
            self.server = FakeSonarrServer(dataset)
        elif name == 'radarr':
            dataset = FakeRadarrDataset()
            dataset.api_key = key
            self.server = FakeRadarrServer(dataset)
        elif name == 'tautulli':
            self.server = FakeTautulliServer(FakeTautulliDataset(api_key=key))
        else:
            dataset = FakeTmdbDataset(access_token=key)
            dataset.movies[550] = tmdb_movie(550, 'Fight Club')
            self.server = FakeTmdbServer(dataset)
        self.dataset = self.server.dataset

    @property
    def base_url(self):
        return self.server.base_url

    @property
    def requests(self):
        return self.server.requests

    def answer_something_else(self):
        """The address answers, but not as this service does."""
        if self.name in ('sonarr', 'radarr'):
            self.dataset.fail_always('GET /api/v3/system/status', 404)
        elif self.name == 'tautulli':
            self.dataset.fail_always('get_users', 404)
        else:
            self.dataset.movies.clear()

    def sent_key(self, key):
        for request in self.requests:
            if key in json.dumps(request):
                return True
        return False


@pytest.fixture(params=NAMES)
def service(request):
    return SERVICES[request.param]


@pytest.fixture
def fake(service):
    running = FakeService(service.name)
    with running.server:
        yield running


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


def point_tmdb_at(config_dir, fake):
    """TMDB's form has no address: tests aim its stored base URL at the fake."""
    if fake.name == 'tmdb':
        update_settings_file(config_dir, {'tmdb_base_url': fake.base_url})


def form_data(service, address, key, origin='connections'):
    data = {service.key_field: key, 'origin': origin}
    if service.url_field:
        data[service.url_field] = address
    return data


def store(config_dir, service, key=KEY, address='http://stored-service:8989'):
    secret_store.store_secret(config_dir, service.key_field, key)
    if service.url_field:
        update_settings_file(config_dir, {service.url_field: address})


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return f'http://127.0.0.1:{port}'


def service_form(html, name):
    match = re.search(rf'<form method="post" action="(/connections/{name}(?:\?[^"]*)?)">(.*?)</form>', html, re.S)
    return match and (match.group(1).split('?')[0], match.group(2))


def resolved_key(config_dir, service):
    return getattr(load_settings(config_dir, env={}), service.key_field)


# -- rule 1: the form -----------------------------------------------------------

@pytest.mark.parametrize('path,origin', [('/connections', 'connections'), ('/setup?step=connections', 'setup')])
def test_each_service_has_a_form_with_address_key_and_help(client, service, path, origin):
    response = client.get(path)
    html = response.get_data(as_text=True)
    found = service_form(html, service.name)
    assert found, f'no form for {service.name}'
    action, body = found
    assert action == f'/connections/{service.name}'
    assert 'name="csrf_token"' in body
    assert f'<input type="hidden" name="origin" value="{origin}"' in body
    if service.url_field:
        assert f'name="{service.url_field}"' in body
    else:
        assert '_base_url' not in body
    password = re.search(rf'<input[^>]*name="{service.key_field}"[^>]*>', body).group(0)
    assert 'type="password"' in password and 'value=' not in password
    assert EN[f'connections.{service.name}.key_help'] in text(response)


def test_an_unknown_service_answers_404_and_changes_nothing(app, client):
    before = snapshot(app)
    for route in ('/connections/emby', '/connections/emby/forget'):
        response = client.post(route, data={'emby_api_key': KEY})
        assert response.status_code == 404
    assert snapshot(app) == before


@pytest.mark.parametrize('suffix', ['', '/forget'])
def test_service_posts_without_the_csrf_token_are_refused(settings, service, suffix):
    from dataclasses import replace
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    store(settings.config_dir, service)
    before = snapshot(app)
    response = app.test_client().post(f'/connections/{service.name}{suffix}',
                                      data=form_data(service, 'http://x:1', OTHER))
    assert response.status_code == 400
    assert snapshot(app) == before


# -- rule 2: validation before anything is stored ---------------------------------

INVALID_ADDRESSES = ['', 'sonarr:8989', 'ftp://sonarr:8989', 'http://', 'http://user:pw@sonarr:8989',
                     'http://sonarr:8989/?x=1', 'http://sonarr:8989/#top', 'http://sonarr:99999',
                     'javascript:alert(1)', 'http://sonarr host:8989']


@pytest.mark.parametrize('name', [name for name in NAMES if SERVICES[name].url_field])
@pytest.mark.parametrize('address', INVALID_ADDRESSES)
def test_an_invalid_address_changes_nothing(app, client, name, address):
    service = SERVICES[name]
    before = snapshot(app)
    response = client.post(f'/connections/{name}', data=form_data(service, address, KEY))
    assert response.status_code == 400
    assert EN['web.form.service_address'] in text(response)
    assert snapshot(app) == before
    assert KEY not in response.get_data(as_text=True)


@pytest.mark.parametrize('locale', ['en', 'ca', 'es'])
def test_an_empty_key_with_none_stored_changes_nothing(app, client, config_dir, service, fake, locale):
    point_tmdb_at(config_dir, fake)
    before = snapshot(app)
    response = client.post(f'/connections/{service.name}?lang={locale}',
                           data=form_data(service, fake.base_url, '  ', origin='setup'))
    assert response.status_code == 400
    assert CATALOGS[locale]['web.form.service_key_required'] in text(response)
    # The wizard re-renders its connections step.
    assert CATALOGS[locale]['setup.connections_intro'] in text(response)
    assert snapshot(app) == before and fake.requests == []


@pytest.mark.parametrize('key', ['two words', 'x' * 513, 'tab\there', 'line\nbreak'])
def test_a_malformed_key_is_refused_before_contacting_the_service(app, client, config_dir, service, fake, key):
    point_tmdb_at(config_dir, fake)
    before = snapshot(app)
    response = client.post(f'/connections/{service.name}', data=form_data(service, fake.base_url, key))
    assert response.status_code == 400
    assert EN['web.form.service_key_invalid'] in text(response)
    assert snapshot(app) == before and fake.requests == []


def test_a_key_of_512_characters_is_accepted(app, client, config_dir, service):
    running = FakeService(service.name, key='k' * 512)
    with running.server:
        point_tmdb_at(config_dir, running)
        response = client.post(f'/connections/{service.name}', data=form_data(service, running.base_url, 'k' * 512))
        assert response.status_code == 303
    assert resolved_key(config_dir, service) == 'k' * 512


# -- rule 3: test before save ---------------------------------------------------------

@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('failure,key', [('unreachable', 'web.form.service_unreachable'),
                                         ('refused', 'web.form.service_key_refused'),
                                         ('other', 'web.form.service_not_service')])
def test_a_failed_test_names_the_service_and_the_failure_and_stores_nothing(
        app, client, config_dir, service, fake, failure, key, stored):
    if stored:
        store(config_dir, service, key=OTHER)
    point_tmdb_at(config_dir, fake)
    address, submitted = fake.base_url, KEY
    if failure == 'unreachable':
        address = closed_port_url()
        if service.name == 'tmdb':
            update_settings_file(config_dir, {'tmdb_base_url': address})
    elif failure == 'refused':
        submitted = 'a-key-the-service-refuses-000'
    else:
        fake.answer_something_else()
    before = snapshot(app)
    response = client.post(f'/connections/{service.name}?lang=ca', data=form_data(service, address, submitted))
    assert response.status_code == 400
    body = text(response)
    expected = CATALOGS['ca'][key].replace('{service}', service.brand)
    assert expected in body
    for other in ('web.form.service_unreachable', 'web.form.service_key_refused', 'web.form.service_not_service'):
        if other != key:
            assert CATALOGS['ca'][other].replace('{service}', service.brand) not in body
    assert snapshot(app) == before
    html = response.get_data(as_text=True)
    assert submitted not in html and OTHER not in html


# -- rules 4 and 5: storage, and never returned in full ------------------------------

def test_a_successful_test_stores_the_address_and_the_encrypted_key(app, client, config_dir, service, fake, caplog):
    secret_store.store_plex_token(config_dir, PLEX_TOKEN)
    point_tmdb_at(config_dir, fake)
    caplog.set_level(logging.DEBUG)
    response = client.post(f'/connections/{service.name}', data=form_data(service, fake.base_url, KEY))
    assert response.status_code == 303
    assert fake.sent_key(KEY)
    settings_file = json.loads((config_dir / 'settings.json').read_text())
    if service.url_field:
        assert settings_file[service.url_field] == fake.base_url
    for path in config_dir.iterdir():
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path.name
    for name in ('secrets.json', 'secrets.key'):
        assert stat.S_IMODE((config_dir / name).stat().st_mode) == 0o600
    assert service.key_field in json.loads((config_dir / 'secrets.json').read_text())
    assert resolved_key(config_dir, service) == KEY
    # Saving one entry keeps every other entry, the Plex token included.
    assert secret_store.read_plex_token(config_dir).token == PLEX_TOKEN
    assert KEY not in caplog.text
    page = client.get(response.headers['Location'])
    html = page.get_data(as_text=True)
    assert EN['web.flash.service_saved'].replace('{service}', service.brand) in text(page)
    assert KEY not in html


def test_the_screens_show_only_a_masked_key(client, config_dir, service):
    store(config_dir, service)
    for path in ('/connections', '/setup?step=connections', '/settings', '/library', '/about'):
        html = client.get(path).get_data(as_text=True)
        assert KEY not in html, path
    connections = client.get('/connections')
    assert '••••' + KEY[-4:] in connections.get_data(as_text=True)
    assert EN['connections.service.key_stored'].replace('{mask}', '••••' + KEY[-4:]) in text(connections)


def test_a_stored_key_makes_the_service_configured(client, config_dir, service):
    store(config_dir, service)
    html = client.get('/connections').get_data(as_text=True)
    row = re.search(rf'<span class="service-name">{service.brand}</span>\s*<span[^>]*status-(\w+)', html)
    assert row and row.group(1) == 'configured'


# -- rule 6: keeping the key ------------------------------------------------------------

def test_an_empty_key_keeps_the_stored_one_and_tests_the_new_address_with_it(app, client, config_dir, service, fake):
    store(config_dir, service)
    point_tmdb_at(config_dir, fake)
    response = client.post(f'/connections/{service.name}', data=form_data(service, fake.base_url, ''))
    assert response.status_code == 303
    assert fake.sent_key(KEY)
    assert resolved_key(config_dir, service) == KEY
    if service.url_field:
        assert json.loads((config_dir / 'settings.json').read_text())[service.url_field] == fake.base_url


# -- rule 7: forget ------------------------------------------------------------------------

def test_forget_deletes_only_that_services_key(client, config_dir, service):
    secret_store.store_plex_token(config_dir, PLEX_TOKEN)
    for other in SERVICES.values():
        store(config_dir, other, key=f'{other.name}-key-1234')
    response = client.post(f'/connections/{service.name}/forget', data={'origin': 'connections'})
    assert response.status_code == 303
    assert resolved_key(config_dir, service) == ''
    for other in SERVICES.values():
        if other is not service:
            assert resolved_key(config_dir, other) == f'{other.name}-key-1234'
    assert secret_store.read_plex_token(config_dir).token == PLEX_TOKEN
    page = client.get(response.headers['Location'])
    assert EN['web.flash.service_forgotten'].replace('{service}', service.brand) in text(page)
    row = re.search(rf'<span class="service-name">{service.brand}</span>\s*<span[^>]*status-(\w+)',
                    page.get_data(as_text=True))
    assert row and row.group(1) == 'not_configured'


# -- rule 8: environment wins, per service --------------------------------------------------

@pytest.mark.parametrize('which', [0, -1])
def test_the_environment_manages_that_service_only(monkeypatch, settings, service, which, owner_client_class):
    from dataclasses import replace
    monkeypatch.setenv(service.env[which], 'http://env-managed:1' if service.env[which].endswith('URL') else 'env-key-9999')
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True, SECRET_KEY='t', WTF_CSRF_ENABLED=False)
    app.test_client_class = owner_client_class
    client = app.test_client()
    store(settings.config_dir, service)
    response = client.get('/connections')
    html = response.get_data(as_text=True)
    assert not service_form(html, service.name)
    assert EN['connections.service.env_managed'].replace('{service}', service.brand) in text(response)
    for other in SERVICES.values():
        if other is not service:
            assert service_form(html, other.name), other.name
    before = snapshot(app)
    for route in (f'/connections/{service.name}', f'/connections/{service.name}/forget'):
        refused = client.post(route, data=form_data(service, 'http://x:1', OTHER))
        assert refused.status_code == 409
    assert snapshot(app) == before


def test_a_blank_environment_variable_counts_as_unset(monkeypatch, config_dir, service):
    store(config_dir, service)
    env = {name: '  ' for name in service.env}
    assert getattr(load_settings(config_dir, env=env), service.key_field) == KEY


def test_the_environment_key_wins_over_the_stored_key(config_dir, service):
    store(config_dir, service)
    env = {service.env[-1]: 'env-key-9999'}
    assert getattr(load_settings(config_dir, env=env), service.key_field) == 'env-key-9999'


# -- rule 9: unreadable keys fail safe -------------------------------------------------------

def test_an_undecryptable_entry_reports_not_configured_and_asks_again(client, config_dir, service, caplog):
    store(config_dir, service)
    secret_store.store_plex_token(config_dir, PLEX_TOKEN)
    payload = json.loads((config_dir / 'secrets.json').read_text())
    payload[service.key_field] = 'not-a-fernet-token'
    (config_dir / 'secrets.json').write_text(json.dumps(payload))
    caplog.set_level(logging.WARNING)
    assert resolved_key(config_dir, service) == ''
    response = client.get('/connections')
    assert response.status_code == 200
    assert EN['connections.service.key_unreadable'].replace('{service}', service.brand) in text(response)
    events = [record for record in caplog.records if record.getMessage() == 'secret_store_unreadable']
    assert any(getattr(record, 'item', None) == service.key_field for record in events)
    assert 'not-a-fernet-token' not in caplog.text
    # The other entries stay readable.
    assert secret_store.read_plex_token(config_dir).token == PLEX_TOKEN


def test_a_key_file_without_a_store_leaves_the_services_not_configured(client, config_dir, service):
    secret_store.ensure_key(config_dir)
    assert resolved_key(config_dir, service) == ''
    response = client.get('/connections')
    assert response.status_code == 200
    assert EN['connections.service.key_unreadable'].replace('{service}', service.brand) not in text(response)


# -- rule 10: existing behaviour holds -------------------------------------------------------

def test_a_store_written_by_an_earlier_version_reads_the_same(config_dir):
    secret_store.store_plex_token(config_dir, PLEX_TOKEN)
    settings = load_settings(config_dir, env={})
    assert settings.plex_token == PLEX_TOKEN and settings.plex_token_status == 'stored'
    for service in SERVICES.values():
        assert getattr(settings, service.key_field) == ''


def test_saving_the_plex_token_keeps_the_service_keys(config_dir, service):
    store(config_dir, service)
    secret_store.store_plex_token(config_dir, PLEX_TOKEN)
    secret_store.forget_plex_token(config_dir)
    assert resolved_key(config_dir, service) == KEY


# -- rule 11: copy ------------------------------------------------------------------------------

def test_the_environment_only_note_is_gone_and_every_new_string_is_translated(client):
    assert 'connections.edit_note' not in EN
    keys = {key for key in EN if key.startswith(('connections.service.', 'web.form.service_', 'web.flash.service_'))
            or re.match(r'connections\.(sonarr|radarr|tautulli|tmdb)\.', key)}
    assert {'connections.service.env_managed', 'web.form.service_address',
            'connections.tmdb.key_help'} <= keys
    for locale in ('ca', 'es'):
        assert keys <= set(CATALOGS[locale]), locale


# -- post-review bounded fixes (R3-001, R3-002, R4-SERVICE-SAVE-NONATOMIC) --------------------

@pytest.mark.parametrize('bad', ['http://sonarr:8989/api?apikey=SECRET-IN-QUERY',
                                 'http://sonarr:8989/#SECRET-IN-FRAG'])
def test_a_rejected_address_never_echoes_query_or_fragment_secrets(app, client, bad):
    response = client.post('/connections/sonarr', data={'sonarr_base_url': bad, 'sonarr_api_key': KEY})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'SECRET-IN-' not in html and KEY not in html


@pytest.mark.parametrize('prior_stored', [False, True])
def test_a_failed_settings_write_rolls_back_the_secret_store(app, client, config_dir, monkeypatch, prior_stored):
    running = FakeService('sonarr', key=KEY)
    if prior_stored:
        store(config_dir, SERVICES['sonarr'], key=OTHER, address='http://old-sonarr:8989')
    before = snapshot(app)
    real_replace = os.replace

    def fail_settings(src, dst):
        if Path(dst).name == 'settings.json':
            raise OSError('disk full')
        return real_replace(src, dst)

    with running.server:
        monkeypatch.setattr(os, 'replace', fail_settings)
        response = client.post('/connections/sonarr', data=form_data(SERVICES['sonarr'], running.base_url, KEY))
        monkeypatch.undo()
    assert response.status_code == 303
    assert EN['web.flash.action_failed'] in text(client.get(response.headers['Location']))
    assert snapshot(app) == before


def test_concurrent_saves_never_cross_pair_address_and_key(app, config_dir, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    first = FakeService('sonarr', key='key-one-1111')
    second = FakeService('sonarr', key='key-two-2222')
    real_replace = os.replace
    gate = threading.Lock()
    delayed = [False]

    def slow_first_settings(src, dst):
        if Path(dst).name == 'settings.json':
            with gate:
                first_call = not delayed[0]
                delayed[0] = True
            if first_call:
                time.sleep(0.1)
        return real_replace(src, dst)

    def submit(fake_srv, key):
        with app.test_client() as c:
            return c.post('/connections/sonarr', data={'sonarr_base_url': fake_srv.base_url, 'sonarr_api_key': key}).status_code

    with first.server, second.server:
        url1, url2 = first.base_url, second.base_url
        monkeypatch.setattr(os, 'replace', slow_first_settings)
        with ThreadPoolExecutor(2) as pool:
            f1 = pool.submit(submit, first, 'key-one-1111')
            time.sleep(0.02)
            f2 = pool.submit(submit, second, 'key-two-2222')
            assert (f1.result(), f2.result()) == (303, 303)
    saved = load_settings(config_dir, env={})
    assert (saved.sonarr_base_url, saved.sonarr_api_key) in {
        (url1, 'key-one-1111'), (url2, 'key-two-2222')}


def test_omitted_key_never_replaces_a_concurrently_updated_key(app, client, config_dir, monkeypatch):
    from almali_library_curator.web import actions
    service = SERVICES['sonarr']
    store(config_dir, service, key=KEY)
    expected = []
    def rotate_during_test(*args):
        store(config_dir, service, key=OTHER, address='http://new-sonarr:8989')
        expected.append(snapshot(app))
        return None
    monkeypatch.setattr(actions, '_test_service', rotate_during_test)
    response = client.post('/connections/sonarr', data={'sonarr_base_url': 'http://old-sonarr:8989'})
    assert response.status_code == 303
    assert snapshot(app) == expected[0]
    assert EN['web.flash.action_failed'] in text(client.get(response.headers['Location']))
