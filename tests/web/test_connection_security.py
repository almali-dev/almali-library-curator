"""Changed service origins must never receive credentials from storage (#61)."""
from html import unescape
from urllib.parse import urlsplit, urlunsplit

import pytest

from almali_library_curator import secret_store
from almali_library_curator.config import load_settings, update_settings_file
from almali_library_curator.db import connect
from almali_library_curator.i18n import translate
from almali_library_curator.web import actions


SECRET = 'stored-credential-not-for-another-origin'
ADDRESS = 'http://service.example:8080/base'
SERVICES = {'plex': 'plex_token', 'sonarr': 'sonarr_api_key',
            'radarr': 'radarr_api_key', 'tautulli': 'tautulli_api_key'}


def snapshot(app):
    settings = app.config['SETTINGS']
    files = {path.name: path.read_bytes() for path in settings.config_dir.iterdir()
             if path.is_file() and path.name != settings.database_path.name}
    db = connect(settings.database_path)
    try:
        return files, list(db.iterdump())
    finally:
        db.close()


def setup_service(app, monkeypatch, service, address=ADDRESS):
    for name in ('PLEX', 'SONARR', 'RADARR', 'TAUTULLI'):
        for suffix in ('BASE_URL', 'TOKEN', 'API_KEY'):
            monkeypatch.delenv(f'ALC_{name}_{suffix}', raising=False)
    directory = app.config['SETTINGS'].config_dir
    secret_store.store_secret(directory, SERVICES[service], SECRET)
    update_settings_file(directory, {service + '_base_url': address})
    calls = []
    monkeypatch.setattr(actions, '_test_plex', lambda *args: calls.append(args))
    monkeypatch.setattr(actions, '_test_service', lambda *args: calls.append(args))
    return directory, calls


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize('change', ['scheme', 'host', 'port', 'missing_stored_address'])
@pytest.mark.parametrize('origin', ['connections', 'setup'])
@pytest.mark.parametrize('locale', ['en', 'ca', 'es'])
def test_changed_origin_refuses_blank_credentials_without_testing_or_saving(
        app, client, monkeypatch, service, change, origin, locale):
    directory, calls = setup_service(
        app, monkeypatch, service, '' if change == 'missing_stored_address' else ADDRESS)
    parts = urlsplit(ADDRESS)
    address = {'scheme': urlunsplit(parts._replace(scheme='https')),
               'host': urlunsplit(parts._replace(netloc='other.example:8080')),
               'port': urlunsplit(parts._replace(netloc='service.example:8081')),
               'missing_stored_address': ADDRESS}[change]
    before = snapshot(app)
    response = client.post(f'/connections/{service}?lang={locale}', data={
        service + '_base_url': address, SERVICES[service]: '  ', 'origin': origin})
    assert response.status_code == 400
    key = 'web.form.plex_address_changed' if service == 'plex' else 'web.form.service_address_changed'
    assert translate(key, locale) in unescape(response.get_data(as_text=True))
    assert SECRET not in response.get_data(as_text=True)
    assert calls == []
    assert snapshot(app) == before
    assert getattr(load_settings(directory, env={}), SERVICES[service]) == SECRET


@pytest.mark.parametrize('service', SERVICES)
@pytest.mark.parametrize('stored,submitted', [
    (ADDRESS, ADDRESS + '/'),
    (ADDRESS, 'http://SERVICE.EXAMPLE:8080/another/path'),
    ('http://service.example', 'http://service.example:80/'),
    ('https://service.example', 'https://service.example:443/'),
    ('http://[::1]:8080', 'http://[::1]:8080/base'),
    ('http://service.example:0', 'http://service.example:0/base'),
])
def test_same_origin_keeps_the_stored_credential(app, client, monkeypatch, service, stored, submitted):
    directory, calls = setup_service(app, monkeypatch, service, stored)
    before = {name: (directory / name).read_bytes() for name in ('secrets.json', 'secrets.key')}
    response = client.post(f'/connections/{service}', data={
        service + '_base_url': submitted, SERVICES[service]: ''})
    assert response.status_code == 303
    assert len(calls) == 1 and calls[0][-1] == SECRET
    loaded = load_settings(directory, env={})
    assert getattr(loaded, service + '_base_url') == submitted.rstrip('/')
    assert getattr(loaded, SERVICES[service]) == SECRET
    assert {name: (directory / name).read_bytes() for name in before} == before


@pytest.mark.parametrize('service', SERVICES)
def test_a_changed_origin_accepts_an_explicit_credential(app, client, monkeypatch, service):
    directory, calls = setup_service(app, monkeypatch, service)
    address, replacement = 'https://new.example:8443', 'explicit-replacement'
    response = client.post(f'/connections/{service}', data={
        service + '_base_url': address, SERVICES[service]: replacement})
    assert response.status_code == 303
    assert len(calls) == 1 and calls[0][-2:] == (address, replacement)
    loaded = load_settings(directory, env={})
    assert getattr(loaded, service + '_base_url') == address
    assert getattr(loaded, SERVICES[service]) == replacement
    assert SECRET not in response.get_data(as_text=True)
    assert replacement not in response.get_data(as_text=True)
