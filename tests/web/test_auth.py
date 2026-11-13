"""S17: login, session cookie, brute-force guard and log redaction (T11).

Everything goes through the Flask test client, the environment and the log
output; no test reaches into the login machinery.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import io
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import parse_qs, urlsplit

from markupsafe import Markup
import pytest
from werkzeug.security import check_password_hash

from almali_library_curator.app import create_app
from almali_library_curator.config import load_settings
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle.safety import kill_switch_enabled
from almali_library_curator.logging_config import configure_logging


LOCALES = ('en', 'ca', 'es')
SCREENS = ['/', '/?tab=protected', '/history', '/history?outcome=deleted', '/history.csv',
           '/connections', '/settings', '/setup', '/setup?step=libraries']
FIFTEEN_MINUTES = 15 * 60


# tests/conftest.py exports the test credentials for every test (S17 rule 1).
def owner_password():
    return os.environ['ALC_AUTH_PASSWORD']


def owner_login(client, user=None, password=None, headers=None, **fields):
    page = client.get('/login').get_data(as_text=True)
    data = {'user': os.environ['ALC_AUTH_USER'] if user is None else user,
            'password': owner_password() if password is None else password, **fields}
    data['csrf_token'] = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return client.post('/login', data=data, headers=headers)


def build(settings, **kwargs):
    app = create_app(replace(settings, scheduler_enabled=False), **kwargs)
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    return app


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def said(key, locale='en'):
    return ' '.join(Markup(translate(key, locale)).striptags().split())


def csrf(client, path='/login'):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def concurrent_login(app, barrier, password):
    client = app.test_client()
    token = csrf(client)
    barrier.wait()
    response = client.post('/login', data={'user': os.environ['ALC_AUTH_USER'], 'password': password, 'csrf_token': token})
    return response.status_code

def session_cookie(response):
    cookies = [value for value in response.headers.getlist('Set-Cookie') if value.startswith('alc_session=')]
    assert len(cookies) == 1, cookies
    return cookies[0]


def without_auth(monkeypatch):
    monkeypatch.delenv('ALC_AUTH_USER', raising=False)
    monkeypatch.delenv('ALC_AUTH_PASSWORD', raising=False)


# -- rule 1: credentials from the environment only ------------------------------


def test_the_password_is_never_stored_kept_on_settings_or_shown(settings):
    app = build(settings)
    client = app.test_client()
    assert owner_login(client).status_code == 303
    for path in settings.config_dir.rglob('*'):
        if path.is_file():
            assert owner_password().encode() not in path.read_bytes(), path
    saved = json.loads((settings.config_dir / 'settings.json').read_text())
    assert not [key for key in saved if 'auth' in key]
    for resolved in (app.config['SETTINGS'], load_settings(settings.config_dir)):
        assert owner_password() not in repr(resolved)
        assert owner_password() not in json.dumps(asdict(resolved), default=str)
    for path in ['/login', *SCREENS]:
        assert owner_password() not in client.get(path).get_data(as_text=True), path


def test_credentials_in_the_settings_file_are_ignored(settings, monkeypatch):
    without_auth(monkeypatch)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / 'settings.json').write_text(
        json.dumps({'auth_user': 'intruder', 'auth_password': 'intruder'}))
    client = build(settings).test_client()
    assert client.post('/login', data={'user': 'intruder', 'password': 'intruder'}).status_code == 503


def test_the_development_example_is_never_a_default(settings, monkeypatch):
    without_auth(monkeypatch)
    app = build(settings)
    app.config['WTF_CSRF_ENABLED'] = False
    response = app.test_client().post('/login', data={'user': 'admin', 'password': 'admin'})
    assert response.status_code == 503


# -- rule 2: fail closed ----------------------------------------------------------


@pytest.mark.parametrize('missing', ['ALC_AUTH_USER', 'ALC_AUTH_PASSWORD', 'both', 'blank'])
@pytest.mark.parametrize('path', ['/login', '/no-such-screen', *SCREENS])
def test_without_a_login_every_screen_answers_the_503_page(settings, monkeypatch, missing, path):
    if missing == 'both':
        without_auth(monkeypatch)
    elif missing == 'blank':
        monkeypatch.setenv('ALC_AUTH_PASSWORD', '  ')
    else:
        monkeypatch.delenv(missing)
    response = build(settings).test_client().get(path)
    assert response.status_code == 503
    assert response.mimetype == 'text/html'
    body = text(response)
    assert 'ALC_AUTH_USER' in body and 'ALC_AUTH_PASSWORD' in body
    assert said('error.auth_missing.title') in body


@pytest.mark.parametrize('locale', LOCALES)
def test_the_503_page_is_translated(settings, monkeypatch, locale):
    without_auth(monkeypatch)
    response = build(settings).test_client().get(f'/?lang={locale}')
    assert response.status_code == 503
    assert f'<html lang="{locale}"' in response.get_data(as_text=True)
    for key in ('error.auth_missing.title', 'error.auth_missing.message'):
        assert said(key, locale) in text(response)


def test_without_a_login_every_action_answers_503_and_changes_nothing(settings, monkeypatch, conn):
    without_auth(monkeypatch)
    app = build(settings)
    client = app.test_client()
    routes = [rule for rule in app.url_map.iter_rules() if 'POST' in rule.methods]
    assert routes
    for rule in routes:
        path = re.sub(r'<(?:int:)?[^>]+>', '1', rule.rule)
        response = client.post(path, data={'enabled': 'on'})
        assert response.status_code == 503, rule.rule
        assert 'ALC_AUTH_USER' in text(response)
    assert kill_switch_enabled(conn) is False


def test_health_stays_alive_and_reports_the_missing_login(settings, monkeypatch):
    without_auth(monkeypatch)
    response = build(settings).test_client().get('/healthz')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'
    assert 'auth' in payload['missing'] and payload['ready'] is False


def test_health_does_not_report_a_configured_login(settings):
    payload = build(settings).test_client().get('/healthz').get_json()
    assert 'auth' not in payload['missing']


# -- rule 3: public routes ----------------------------------------------------------


def test_only_health_static_and_login_are_public(settings):
    client = build(settings).test_client()
    assert client.get('/healthz').status_code == 200
    assert client.get('/static/css/hud.css').status_code == 200
    assert client.get('/login').status_code == 200


@pytest.mark.parametrize('path', [*SCREENS, '/no-such-screen'])
def test_an_anonymous_screen_redirects_to_the_login_with_next(settings, path):
    response = build(settings).test_client().get(path)
    assert response.status_code == 303
    location = urlsplit(response.headers['Location'])
    assert location.path == '/login' and not location.netloc
    assert parse_qs(location.query)['next'] == [path]


def test_an_anonymous_form_action_goes_to_the_login_and_changes_nothing(settings, conn):
    client = build(settings).test_client()
    token = csrf(client)
    response = client.post('/settings/kill-switch', data={'enabled': 'on', 'csrf_token': token})
    assert response.status_code == 303
    assert urlsplit(response.headers['Location']).path == '/login'
    assert kill_switch_enabled(conn) is False


def test_the_json_scan_answers_401_json_without_a_session(settings):
    client = build(settings).test_client()
    token = csrf(client)
    response = client.post('/scan', json={}, headers={'X-CSRFToken': token})
    assert response.status_code == 401
    assert response.is_json and response.get_json()['error']


def test_the_login_honours_a_relative_next(settings):
    client = build(settings).test_client()
    response = owner_login(client, next='/history?outcome=deleted')
    assert response.status_code == 303
    assert response.headers['Location'] == '/history?outcome=deleted'


@pytest.mark.parametrize('target', ['https://evil.example/', '//evil.example/x', '///evil.example',
                                    'javascript:alert(1)', 'http:/evil.example', '/\\evil.example',
                                    'evil.example', ''])
def test_an_open_redirect_next_lands_on_library_health(settings, target):
    response = owner_login(build(settings).test_client(), next=target)
    assert response.status_code == 303
    location = urlsplit(response.headers['Location'])
    assert not location.scheme and not location.netloc
    assert location.path == '/'


# -- rule 4: login and logout -------------------------------------------------------


@pytest.mark.parametrize('locale', LOCALES)
def test_the_login_form_is_translated(settings, locale):
    response = build(settings).test_client().get(f'/login?lang={locale}')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert re.search(r'<input[^>]*name="user"', html)
    password = re.search(r'<input[^>]*name="password"[^>]*>', html).group(0)
    assert 'type="password"' in password and 'value=' not in password
    assert 'name="csrf_token"' in html
    for key in ('login.title', 'login.user', 'login.password', 'login.submit'):
        assert said(key, locale) in text(response)


def test_a_successful_login_opens_the_screens(settings):
    client = build(settings).test_client()
    response = owner_login(client)
    assert response.status_code == 303 and response.headers['Location'] == '/'
    assert client.get('/').status_code == 200


@pytest.mark.parametrize('user,password', [('someone', None), (None, 'wrong'),
                                           ('someone', 'wrong'), ('', '')])
def test_a_failed_login_says_one_thing_and_never_which_field(settings, user, password):
    client = build(settings).test_client()
    response = owner_login(client, user=user, password=password)
    assert response.status_code == 401
    body = text(response)
    assert said('login.failed') in body
    assert client.get('/').status_code == 303


def test_wrong_user_and_wrong_password_answer_the_same_page(settings):
    def page(user, password):
        html = owner_login(build(settings).test_client(), user=user, password=password).get_data(as_text=True)
        return re.sub(r'name="csrf_token" value="[^"]+"', '', html)
    assert page('someone', owner_password()) == page(os.environ['ALC_AUTH_USER'], 'wrong')


def test_the_login_clears_the_old_session(settings):
    client = build(settings).test_client()
    client.get('/login')
    with client.session_transaction() as session:
        session['planted'] = 'by someone else'
    assert owner_login(client).status_code == 303
    with client.session_transaction() as session:
        assert 'planted' not in session


def test_one_session_is_valid_on_every_worker_and_after_a_restart(settings):
    # Review R3-session-proof-per-process: Gunicorn workers and restarts build
    # their own app with the same signing key and the same credentials; a
    # session issued by one must be accepted by the others.
    first = build(settings).test_client()
    assert owner_login(first).status_code == 303
    cookie = first.get_cookie('alc_session')
    second = build(settings).test_client()
    second.set_cookie('alc_session', cookie.value)
    assert second.get('/').status_code == 200


def test_a_new_password_signs_the_old_session_out(settings, monkeypatch):
    first = build(settings).test_client()
    assert owner_login(first).status_code == 303
    cookie = first.get_cookie('alc_session')
    monkeypatch.setenv('ALC_AUTH_PASSWORD', owner_password() + '-changed')
    restarted = build(settings).test_client()
    restarted.set_cookie('alc_session', cookie.value)
    assert restarted.get('/').status_code == 303


def test_logout_clears_the_session(settings):
    client = build(settings).test_client()
    owner_login(client)
    token = csrf(client, '/')
    response = client.post('/logout', data={'csrf_token': token})
    assert response.status_code == 303
    assert urlsplit(response.headers['Location']).path == '/login'
    assert client.get('/').status_code == 303


@pytest.mark.parametrize('locale', LOCALES)
def test_the_layout_shows_a_logout_button(settings, locale):
    client = build(settings).test_client()
    owner_login(client)
    html = client.get(f'/?lang={locale}').get_data(as_text=True)
    form = re.search(r'<form method="post" action="/logout[^"]*">(.*?)</form>', html, re.S)
    assert form, 'no logout form'
    assert 'name="csrf_token"' in form.group(1)
    assert translate('auth.logout', locale) in form.group(1)


# -- rule 5: session cookie -------------------------------------------------------


def test_the_session_cookie_is_httponly_lax_permanent_and_not_secure_by_default(settings):
    response = owner_login(build(settings).test_client())
    cookie = session_cookie(response)
    parts = [part.strip() for part in cookie.split(';')]
    assert 'HttpOnly' in parts and 'SameSite=Lax' in parts
    assert 'Secure' not in parts
    expires = parsedate_to_datetime(next(p for p in parts if p.startswith('Expires='))[8:])
    lifetime = expires - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=58) < lifetime <= timedelta(hours=12)


def test_secure_cookies_turn_on_from_the_environment(settings, monkeypatch):
    monkeypatch.setenv('ALC_SECURE_COOKIES', 'true')
    response = owner_login(build(settings).test_client())
    assert 'Secure' in [part.strip() for part in session_cookie(response).split(';')]


def test_a_short_signing_key_is_replaced_and_kept_private(settings):
    settings.config_dir.mkdir(parents=True)
    key_file = settings.config_dir / 'secret_key'
    key_file.write_bytes(b'short')
    key_file.chmod(0o644)
    app = build(settings)
    assert len(app.config['SECRET_KEY']) >= 32
    assert key_file.read_bytes() == app.config['SECRET_KEY']
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert build(settings).config['SECRET_KEY'] == app.config['SECRET_KEY']


def test_a_valid_signing_key_is_kept_and_made_private(settings):
    settings.config_dir.mkdir(parents=True)
    key_file = settings.config_dir / 'secret_key'
    key_file.write_bytes(b'k' * 48)
    key_file.chmod(0o644)
    assert build(settings).config['SECRET_KEY'] == b'k' * 48
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


# -- rule 6: brute-force guard ------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def guarded(settings):
    clock = Clock()
    return build(settings, clock=clock), clock


def fail(client, times):
    for _ in range(times):
        assert owner_login(client, password='wrong').status_code == 401


def test_five_failures_lock_the_address_for_fifteen_minutes(settings):
    app, clock = guarded(settings)
    client = app.test_client()
    fail(client, 5)
    clock.now += FIFTEEN_MINUTES - 1
    locked = owner_login(client)  # even the right password is not checked
    assert locked.status_code == 429
    assert said('login.locked') in text(locked)
    assert client.get('/').status_code == 303
    clock.now += 2
    assert owner_login(client).status_code == 303


@pytest.mark.parametrize('locale', LOCALES)
def test_the_lockout_message_is_translated(settings, locale):
    app, _ = guarded(settings)
    client = app.test_client()
    fail(client, 5)
    page = client.get(f'/login?lang={locale}').get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = client.post(f'/login?lang={locale}', data={
        'user': os.environ['ALC_AUTH_USER'], 'password': owner_password(), 'csrf_token': token})
    assert response.status_code == 429
    assert said('login.locked', locale) in text(response)


def test_other_addresses_are_not_locked(settings):
    app, _ = guarded(settings)
    fail(app.test_client(), 5)
    other = app.test_client()
    other.environ_base['REMOTE_ADDR'] = '192.0.2.7'
    assert owner_login(other).status_code == 303


def test_failures_older_than_fifteen_minutes_do_not_count(settings):
    app, clock = guarded(settings)
    client = app.test_client()
    fail(client, 4)
    clock.now += FIFTEEN_MINUTES + 1
    fail(client, 1)
    assert owner_login(client).status_code == 303


def test_a_successful_login_clears_the_failures(settings):
    app, _ = guarded(settings)
    client = app.test_client()
    fail(client, 4)
    assert owner_login(client).status_code == 303
    fail(client, 4)
    assert owner_login(client).status_code == 303


def test_overlapping_logins_check_at_most_five_passwords(settings, monkeypatch):
    app = build(settings)
    release, fifth = threading.Event(), threading.Event()
    checks = []

    def blocking_check(password_hash, candidate):
        checks.append(candidate)
        if len(checks) == 5:
            fifth.set()
        release.wait(5)
        return check_password_hash(password_hash, candidate)

    monkeypatch.setattr('almali_library_curator.auth.check_password_hash', blocking_check)
    barrier = threading.Barrier(10)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(concurrent_login, app, barrier, 'wrong') for _ in range(10)]
        assert fifth.wait(5)
        release.set()
        statuses = [future.result() for future in futures]
    assert len(checks) == 5
    assert sorted(statuses) == [401] * 5 + [429] * 5


def test_an_in_flight_success_does_not_clear_a_concurrent_lock(settings, monkeypatch):
    app = build(settings)
    entered, release = threading.Event(), threading.Event()

    def blocking_check(password_hash, candidate):
        if candidate == os.environ['ALC_AUTH_PASSWORD']:
            entered.set()
            release.wait(5)
        return check_password_hash(password_hash, candidate)

    monkeypatch.setattr('almali_library_curator.auth.check_password_hash', blocking_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(concurrent_login, app, threading.Barrier(1), os.environ['ALC_AUTH_PASSWORD'])
        assert entered.wait(5)
        wrong_statuses = [concurrent_login(app, threading.Barrier(1), 'wrong')
                          for _ in range(5)]
        release.set()
        assert future.result() == 303
    assert wrong_statuses == [401, 401, 401, 401, 429]
    assert owner_login(app.test_client()).status_code == 429


# -- rule 7: log redaction ------------------------------------------------------------


SENSITIVE = {'plex_token': 't-1', 'sonarr_api_key': 'k-2', 'password': 'p-3', 'client_secret': 's-4',
             'Authorization': 'a-5', 'set_cookie': 'c-6', 'tmdb_apikey': 'z-7', 'API_KEY': 'u-8'}


def test_the_root_filter_masks_sensitive_fields():
    stream = io.StringIO()
    configure_logging('INFO', stream=stream)
    try:
        logging.getLogger('almali_library_curator.some.module').info(
            'event', extra={**SENSITIVE, 'client': '192.0.2.1'})
    finally:
        configure_logging('INFO')
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    for name, value in SENSITIVE.items():
        assert record[name] == '***', name
        assert value not in stream.getvalue()
    assert record['client'] == '192.0.2.1'


@pytest.mark.parametrize('user, password', [('admin', 'admin'), ('owner-my-long-secret', 'my-long-secret')])
def test_success_log_masks_a_user_that_contains_the_password(settings, monkeypatch, caplog, user, password):
    monkeypatch.setenv('ALC_AUTH_USER', user); monkeypatch.setenv('ALC_AUTH_PASSWORD', password)
    caplog.set_level(logging.INFO)
    client = build(settings).test_client()
    assert owner_login(client, user=user, password=password).status_code == 303
    record = next(item for item in caplog.records if item.getMessage() == 'auth_login_succeeded')
    assert record.user == '***'


def test_a_whole_login_run_never_logs_the_password(settings, caplog, capsys):
    caplog.set_level(logging.INFO)
    app, clock = guarded(settings)
    client = app.test_client()
    owner_login(client, password='wrong')
    assert owner_login(client).status_code == 303
    client.post('/logout', data={'csrf_token': csrf(client, '/')})
    fail(client, 5)
    assert owner_login(client).status_code == 429
    output = capsys.readouterr().out
    assert owner_password() not in caplog.text and owner_password() not in output
    assert all(owner_password() not in str(value)
               for record in caplog.records for value in vars(record).values())
    events = {}
    for record in caplog.records:
        events.setdefault(record.getMessage(), []).append(record)
    for name in ('auth_login_failed', 'auth_login_succeeded', 'auth_logout', 'auth_login_locked'):
        assert name in events, name
        assert all(record.client == '127.0.0.1' for record in events[name])
    assert all(record.user == os.environ['ALC_AUTH_USER'] for record in events['auth_login_succeeded'])
    for name in ('auth_login_failed', 'auth_logout', 'auth_login_locked'):
        assert not any(hasattr(record, 'user') for record in events[name]), name
    # The JSON lines on the root handler carry the events too.
    assert '"auth_login_locked"' in output


# -- rule 10: trusted reverse proxies --------------------------------------------------


@pytest.mark.parametrize('value,expected', [('0', 429), ('1', 401), ('not-an-integer', 429), ('-1', 429)])
def test_forwarded_addresses_follow_the_trusted_proxy_setting(
        settings, monkeypatch, caplog, value, expected):
    monkeypatch.setenv('ALC_TRUSTED_PROXIES', value)
    caplog.set_level(logging.WARNING)
    client = build(settings).test_client()
    client.environ_base['REMOTE_ADDR'] = '192.0.2.10'
    first = {'X-Forwarded-For': '198.51.100.1'}
    for _ in range(5):
        assert owner_login(client, password='wrong', headers=first).status_code == 401
    second = {'X-Forwarded-For': '198.51.100.2'}
    assert owner_login(client, password='wrong', headers=second).status_code == expected
    if value in {'not-an-integer', '-1'}:
        assert any(item.getMessage() == 'invalid_trusted_proxies' for item in caplog.records)


# -- rule 9: copy and docs ---------------------------------------------------------------


AUTH_KEYS = ('login.title', 'login.intro', 'login.user', 'login.password', 'login.submit',
             'login.failed', 'login.locked', 'auth.logout',
             'error.auth_missing.title', 'error.auth_missing.message')


@pytest.mark.parametrize('locale', LOCALES)
def test_every_new_string_exists_in_every_locale(locale):
    catalog = json.loads((Path(__file__).resolve().parents[2] / 'almali_library_curator' / 'i18n'
                          / f'{locale}.json').read_text())
    for key in AUTH_KEYS:
        assert catalog.get(key, '').strip(), key
    assert 'ALC_AUTH_USER' in catalog['error.auth_missing.message']
    assert 'ALC_AUTH_PASSWORD' in catalog['error.auth_missing.message']


def test_the_readme_documents_the_login():
    readme = (Path(__file__).resolve().parents[2] / 'README.md').read_text()
    section = readme[readme.index('## Login'):]
    section = section[:section.find('\n## ', 4)] if '\n## ' in section[4:] else section
    for needle in ('ALC_AUTH_USER', 'ALC_AUTH_PASSWORD', 'ALC_SECURE_COOKIES', '503',
                   'ALC_AUTH_USER=admin', 'ALC_AUTH_PASSWORD=admin'):
        assert needle in section, needle
