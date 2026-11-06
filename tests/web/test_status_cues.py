"""T10 gap (d): every badge-like or status element carries text and an icon.

S7: "Every badge has text and an icon; colour is never the only cue." The two
badge macros are covered in tests/ui/test_shell.py; this test reads the
rendered screens, so it also covers the markup outside them: the Plex
"Required" badge, connection statuses, the re-download warning, row errors,
form field errors, flashed messages and the Plex sign-in states.
"""
import json
from html.parser import HTMLParser

import pytest

from demo_data import configure_arrs, seed_demo

# Classes and roles that mark a status: each must show an icon and words.
STATUS_CLASSES = ('badge', 'row-warning', 'row-error', 'field-error', 'flash-message', 'empty-state')
STATUS_ROLES = ('status', 'alert', 'note')
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


class _StatusElements(HTMLParser):
    """Every status element with its own text and whether it holds an icon."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.open = []  # [tag, description, text parts, has icon]
        self.found = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'use' and (attrs.get('href') or '').startswith('/static/icons.svg#'):
            for element in self.open:
                if element[1]:
                    element[3] = True
        if tag in VOID:
            return
        classes = (attrs.get('class') or '').split()
        is_status = any(name in classes for name in STATUS_CLASSES) or attrs.get('role') in STATUS_ROLES
        description = f"<{tag} class={attrs.get('class')!r} role={attrs.get('role')!r}>" if is_status else None
        self.open.append([tag, description, [], False])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        while self.open:
            element = self.open.pop()
            if element[1]:
                self.found.append((element[1], ' '.join(''.join(element[2]).split()), element[3]))
            if element[0] == tag:
                break

    def handle_data(self, data):
        for element in self.open:
            if element[1]:
                element[2].append(data)


def statuses(response):
    return _StatusElements(response.get_data(as_text=True)).found


@pytest.fixture
def demo(app, conn, monkeypatch):
    configure_arrs(monkeypatch)
    seed_demo(app, conn)
    return app


def _signin(app, state, **extra):
    path = app.config['SETTINGS'].config_dir / 'plex-signin.json'
    path.write_text(json.dumps({'version': 1, 'state': state, 'pin_id': 7, 'code': 'ABCD',
                                'client_identifier': 'client', **extra}))


PAGES = {
    'candidates': lambda client: client.get('/?lang={locale}'),
    'leaving_soon': lambda client: client.get('/?tab=leaving_soon&lang={locale}'),
    'protected': lambda client: client.get('/?tab=protected&lang={locale}'),
    'history': lambda client: client.get('/history?lang={locale}'),
    'connections': lambda client: client.get('/connections?lang={locale}'),
    'setup_plex': lambda client: client.get('/setup?step=plex&lang={locale}'),
    'setup_connections': lambda client: client.get('/setup?step=connections&lang={locale}'),
    'settings_errors': lambda client: client.post('/settings?lang={locale}', data={
        'grace_days': '2', 'scan_time': '25:00', 'collection_name': ' ', 'minimum_rating': '11'}),
    'plex_form_errors': lambda client: client.post('/connections/plex?lang={locale}', data={
        'plex_base_url': 'ftp://nope', 'plex_token': 'bad token', 'origin': 'connections'}),
    'flash': lambda client: client.post('/settings/kill-switch?lang={locale}', data={'enabled': 'on'},
                                        follow_redirects=True),
}


@pytest.mark.parametrize('locale', ['en', 'ca', 'es'])
@pytest.mark.parametrize('screen', sorted(PAGES))
def test_every_status_element_on_every_screen_has_an_icon_and_text(demo, client, screen, locale):
    response = PAGES[screen](_LocaleClient(client, locale))
    found = statuses(response)
    assert found, screen
    missing = [(element, words) for element, words, icon in found if not icon or not words.strip()]
    assert not missing


@pytest.mark.parametrize('state,extra', [
    ('pending', {}), ('authorized', {}), ('cancelled', {}),
    ('succeeded', {'message': 'web.flash.plex_saved'}),
    ('failed', {'message': 'web.form.plex_signin_expired'}),
])
def test_every_plex_sign_in_state_has_an_icon_and_text(demo, client, state, extra):
    _signin(demo, state, **extra)
    found = statuses(client.get('/connections?lang=ca'))
    assert any(role_status for role_status, _, _ in found if "role='status'" in role_status
               or "role='alert'" in role_status)
    assert not [(element, words) for element, words, icon in found if not icon or not words]


def test_the_required_plex_badge_has_an_icon_and_its_word(demo, client):
    found = statuses(client.get('/connections?lang=en'))
    required = [entry for entry in found if 'badge-required' in entry[0]]
    assert required and all(icon and words == 'Required' for _, words, icon in required)


class _LocaleClient:
    """Fills the ``{locale}`` placeholder of the page recipes above."""

    def __init__(self, client, locale):
        self.client, self.locale = client, locale

    def get(self, url, **kwargs):
        return self.client.get(url.format(locale=self.locale), **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url.format(locale=self.locale), **kwargs)
