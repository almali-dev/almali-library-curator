"""Baseline browser protections cover pages, APIs, redirects and errors (#62)."""
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import pytest

from almali_library_curator.app import create_app
from test_auth import owner_login


HEADERS = {
    'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'",
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin',
    'Cache-Control': 'no-store',
}


@pytest.mark.parametrize('header,value', HEADERS.items())
@pytest.mark.parametrize('method,path,status', [
    ('get', '/', 200), ('get', '/login', 200), ('get', '/healthz', 200),
    ('get', '/history.csv', 200), ('get', '/missing-page', 404),
    ('post', '/healthz', 405), ('post', '/scan', 202),
])
def test_non_static_responses_have_security_headers(app, client, monkeypatch, header, value, method, path, status):
    monkeypatch.setattr(app.extensions['scan_runner'], 'start_background', lambda: True)
    response = getattr(client, method)(path, buffered=True)
    assert response.status_code == status
    assert response.headers.getlist(header) == [value]


@pytest.mark.parametrize('scenario,status', [('redirect', 303), ('csrf', 400),
                                           ('unconfigured', 503), ('server', 500)])
def test_refusals_and_errors_also_have_headers(settings, monkeypatch, scenario, status):
    if scenario == 'unconfigured':
        monkeypatch.delenv('ALC_AUTH_PASSWORD', raising=False)
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    if scenario == 'server':
        def fail():
            raise RuntimeError('not shown')
        app.view_functions['healthz'] = fail
    client = app.test_client()
    response = client.post('/scan') if scenario == 'csrf' else client.get(
        '/healthz' if scenario == 'server' else '/')
    assert response.status_code == status
    for header, value in HEADERS.items():
        assert response.headers.getlist(header) == [value]


def test_static_assets_keep_their_cache_and_conditional_responses(client):
    response = client.get('/static/css/hud.css')
    assert response.status_code == 200
    assert 'no-store' not in response.headers.get('Cache-Control', '')
    for header, value in HEADERS.items():
        if header != 'Cache-Control':
            assert response.headers.getlist(header) == [value]
    cached = client.get('/static/css/hud.css', headers={'If-None-Match': response.headers['ETag']})
    assert cached.status_code == 304
    assert 'no-store' not in cached.headers.get('Cache-Control', '')


@pytest.mark.parametrize('secure', [False, True])
def test_the_named_cookie_keeps_its_security_flags_and_login_works(settings, monkeypatch, secure):
    monkeypatch.setenv('ALC_SECURE_COOKIES', str(secure).lower())
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config['TESTING'] = True
    client = app.test_client()
    response = owner_login(client)
    assert response.status_code == 303
    cookies = response.headers.getlist('Set-Cookie')
    assert len(cookies) == 1 and cookies[0].startswith('alc_session=')
    assert 'HttpOnly' in cookies[0] and 'SameSite=Lax' in cookies[0]
    assert ('; Secure' in cookies[0]) == secure
    assert client.get_cookie('session') is None
    assert client.get_cookie('alc_session') is not None
    assert client.get('/').status_code == 200


class InlineContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.violations = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'style' or (tag == 'script' and not attrs.get('src')):
            self.violations.append(tag)
        for name, value in attrs.items():
            if name == 'style' or name.startswith('on') or (value or '').lower().startswith('javascript:'):
                self.violations.append(name)


def test_all_templates_are_free_of_inline_scripts_styles_and_handlers():
    root = Path(__file__).resolve().parents[2] / 'almali_library_curator'
    templates = [*root.joinpath('templates').rglob('*.html'),
                 *root.joinpath('web/templates').rglob('*.html')]
    assert templates
    for path in templates:
        parser = InlineContent()
        parser.feed(path.read_text())
        assert parser.violations == [], str(path)
