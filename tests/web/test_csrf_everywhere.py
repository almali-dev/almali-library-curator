"""S17 rule 8: every mutating route refuses a post without the CSRF token.

The routes come from the application's own URL map, so a new POST route is
covered the moment it exists; ``/login`` and ``/logout`` are included.
"""
from dataclasses import replace
import os
from pathlib import Path
import re
import tempfile

import pytest

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings
from almali_library_curator.db import connect


def mutating_rules(app):
    return sorted(rule.rule for rule in app.url_map.iter_rules()
                  if rule.methods & {'POST', 'PUT', 'PATCH', 'DELETE'})


def _collected_rules():
    with tempfile.TemporaryDirectory() as directory:
        return mutating_rules(create_app(Settings(config_dir=Path(directory), scheduler_enabled=False)))


RULES = _collected_rules()


def snapshot(settings):
    files = {path.name: path.read_bytes() for path in sorted(settings.config_dir.glob('*'))
             if path.is_file() and not path.name.startswith('almali-library-curator.db')}
    conn = connect(settings.database_path)
    try:
        return files, list(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture
def signed_in(settings):
    """A real owner session over an app with CSRF on, as in production."""
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)
    client = app.test_client()
    page = client.get('/login').get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = client.post('/login', data={'user': os.environ['ALC_AUTH_USER'],
                                           'password': os.environ['ALC_AUTH_PASSWORD'],
                                           'csrf_token': token})
    assert response.status_code == 303
    return app, client


def test_the_sweep_covers_every_mutating_route_of_the_live_app(signed_in):
    app, _ = signed_in
    assert mutating_rules(app) == RULES
    assert {'/login', '/logout', '/scan', '/settings/kill-switch'} <= set(RULES)


@pytest.mark.parametrize('rule', RULES)
def test_a_post_without_the_token_is_refused_with_nothing_changed(settings, signed_in, rule):
    app, client = signed_in
    path = re.sub(r'<(?:\w+:)?\w+>', '1', rule)
    before = snapshot(settings)
    response = client.post(path, data={'enabled': 'on', 'user': 'x', 'password': 'y',
                                       'plex_base_url': 'http://192.0.2.1:32400', 'plex_token': 'abcd'})
    assert response.status_code == 400
    assert snapshot(settings) == before
    # The session survived: a refused /logout or /login changed nothing either.
    assert client.get('/settings').status_code == 200
