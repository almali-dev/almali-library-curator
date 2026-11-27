"""S2 (#145): a one mebibyte request body limit, answered before parsing.

No route reads an upload: every action reads a small URL-encoded form, so a
request body past 1 MiB is never legitimate. The limit keeps an unauthenticated
client from spooling a large body to the container's small ``/tmp`` tmpfs, and
the refusal is answered before the CSRF check or the login check reads the
form, so an oversized request changes nothing.
"""
from __future__ import annotations

from dataclasses import replace
from html import unescape
import os
from pathlib import Path
import re

from almali_library_curator.app import MAX_CONTENT_LENGTH, create_app
from almali_library_curator.i18n import load_catalogs
from almali_library_curator.lifecycle.safety import kill_switch_enabled

EN = load_catalogs()['en']
CA = load_catalogs()['ca']
PACKAGE = Path(__file__).resolve().parents[2] / 'almali_library_curator'


def build(settings, **kwargs):
    app = create_app(replace(settings, scheduler_enabled=False), **kwargs)
    app.config.update(TESTING=True)  # CSRF stays on, as in production
    return app


def owner():
    return os.environ['ALC_AUTH_USER'], os.environ['ALC_AUTH_PASSWORD']


def page_text(response):
    stripped = re.sub(r'<[^>]+>', ' ', response.get_data(as_text=True))
    return ' '.join(unescape(stripped).split())


def csrf(client, path='/login'):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def sign_in(client):
    user, password = owner()
    return client.post('/login', data={
        'user': user, 'password': password, 'csrf_token': csrf(client)})


def form_body(token=None, size=MAX_CONTENT_LENGTH):
    """A URL-encoded body of exactly ``size`` bytes."""
    user, _ = owner()
    fields = {'user': user, 'password': 'wrong-password'}
    if token:
        fields['csrf_token'] = token
    body = '&'.join(f'{name}={value}' for name, value in fields.items())
    return body + '&padding=' + 'x' * (size - len(body) - len('&padding='))


def test_the_limit_is_the_documented_one_mebibyte():
    assert MAX_CONTENT_LENGTH == 1024 * 1024


def test_an_oversized_signed_out_post_is_refused_before_the_csrf_check(settings):
    """No token, no session: 413, never the 400 of a refused CSRF token."""
    client = build(settings).test_client()
    before = sorted(settings.config_dir.glob('*'))

    response = client.post('/login', data=form_body(size=MAX_CONTENT_LENGTH + 1),
                           content_type='application/x-www-form-urlencoded')

    assert response.status_code == 413
    body = page_text(response)
    assert EN['error.too_large.title'] in body
    assert 'Request Entity Too Large' not in body
    assert 'csrf' not in body.lower()
    # Nothing changed: no file, no session, and the answer is not cached.
    assert sorted(settings.config_dir.glob('*')) == before
    assert 'alc_session' not in response.headers.get('Set-Cookie', '')
    assert response.headers['Cache-Control'] == 'no-store'


def test_an_oversized_signed_in_post_with_a_valid_token_is_refused_too(settings, conn):
    """The CSRF check lets the request through; the limit still wins."""
    client = build(settings).test_client()
    assert sign_in(client).status_code == 303

    response = client.post('/settings/kill-switch',
                           data=form_body(csrf(client, '/settings'),
                                          size=MAX_CONTENT_LENGTH + 1),
                           content_type='application/x-www-form-urlencoded')

    assert response.status_code == 413
    # The body was never parsed, so the action never ran.
    assert kill_switch_enabled(conn) is False


def test_a_body_of_exactly_the_limit_is_read_normally(settings):
    """The limit cuts past 1 MiB only: a body of exactly the limit is parsed."""
    client = build(settings).test_client()

    response = client.post('/login', data=form_body(csrf(client)),
                           content_type='application/x-www-form-urlencoded')

    assert response.status_code == 401


def test_a_normal_signed_in_form_still_works(settings, conn):
    """Every screen posts a tiny form; the limit never gets in the way."""
    client = build(settings).test_client()
    assert sign_in(client).status_code == 303

    response = client.post('/settings/kill-switch', data={
        'enabled': 'on', 'csrf_token': csrf(client, '/settings')})

    assert response.status_code == 303
    assert kill_switch_enabled(conn) is True


def test_the_app_offers_no_upload_for_the_limit_to_break():
    """There is no import or upload route: every body is a small form."""
    sources = [*PACKAGE.rglob('*.py'), *PACKAGE.rglob('*.html')]
    assert [str(path.relative_to(PACKAGE)) for path in sources
            if 'request.files' in path.read_text()
            or 'enctype="multipart/form-data"' in path.read_text()] == []


def test_the_oversized_answer_is_the_app_error_page_in_the_reader_language(settings):
    """Like 404, 405, CSRF and 500, the 413 is the app's own translated page."""
    client = build(settings).test_client()

    response = client.post('/login?lang=ca', data=form_body(size=MAX_CONTENT_LENGTH + 1),
                           content_type='application/x-www-form-urlencoded')

    assert response.status_code == 413
    body = page_text(response)
    assert CA['error.too_large.title'] in body
    assert CA['error.too_large.message'] in body
    assert response.headers['Content-Security-Policy'] == "default-src 'self'; frame-ancestors 'none'"
