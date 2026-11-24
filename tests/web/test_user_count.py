"""#122: the app counts and lists the people who watch, not every Plex account."""

from dataclasses import replace
from datetime import datetime, timezone
import re

from almali_library_curator.config import save_settings
from almali_library_curator.web.views import sources_view
from lifecycle_helpers import seed_play, seed_user

PLAYED = datetime(2024, 5, 1, tzinfo=timezone.utc)


def seed_server(conn):
    """Four people with plays (one excluded in the database, one by name in
    Settings) and many accounts that never played anything."""
    for name in ('Marc', 'Anna', 'Joe', 'Pau'):
        user_id = seed_user(conn, name.lower(), name, excluded=name == 'Pau')
        seed_play(conn, user_id, '100', PLAYED)
    for index in range(50):
        seed_user(conn, f'friend{index}', f'Friend {index}')


def text(response):
    return re.sub(r'\s+', ' ', response.get_data(as_text=True))


def test_sources_counts_only_included_users_with_a_play(app, client, conn):
    seed_server(conn)
    save_settings(replace(app.config['SETTINGS'], excluded_users=('joe',)))

    assert 'Plex · 2 users' in text(client.get('/'))


def test_sources_without_settings_still_skips_excluded_and_silent_users(conn):
    seed_server(conn)

    assert sources_view(conn).plex_users == 3


def test_settings_lists_users_with_plays_and_every_excluded_user(app, client, conn):
    seed_server(conn)
    seed_user(conn, 'old', 'Old Guest')
    save_settings(replace(app.config['SETTINGS'], excluded_users=('old guest',)))

    body = text(client.get('/settings'))
    listed = re.findall(r'name="excluded_users" value="([^"]+)"', body)

    assert sorted(listed) == ['Anna', 'Joe', 'Marc', 'Old Guest', 'Pau']
    assert 'Friend 0' not in body


def test_settings_without_any_play_shows_the_empty_message(client, conn):
    seed_user(conn, 'friend', 'Friend')

    body = text(client.get('/settings'))

    assert 'name="excluded_users" value="Friend"' not in body
    assert 'No users with a recorded play yet.' in body
