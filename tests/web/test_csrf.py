"""Every mutating route needs the CSRF token the screens embed (S9)."""
from dataclasses import replace
import re
import stat

from almali_library_curator.app import create_app
from almali_library_curator.lifecycle.safety import kill_switch_enabled


def protected_client(settings):
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    return app.test_client()


def test_a_post_without_the_token_is_refused_and_changes_nothing(settings, conn):
    client = protected_client(settings)
    response = client.post('/settings/kill-switch', data={'enabled': 'on'})
    assert response.status_code == 400
    assert kill_switch_enabled(conn) is False


def test_a_post_with_the_token_from_the_page_is_accepted(settings, conn):
    client = protected_client(settings)
    page = client.get('/settings').get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = client.post('/settings/kill-switch', data={'enabled': 'on', 'csrf_token': token})
    assert response.status_code == 303
    assert kill_switch_enabled(conn) is True


def test_the_signing_key_survives_a_restart_and_is_private(settings):
    first = create_app(replace(settings, scheduler_enabled=False)).config['SECRET_KEY']
    second = create_app(replace(settings, scheduler_enabled=False)).config['SECRET_KEY']
    assert first == second and len(first) >= 32
    key_file = settings.config_dir / 'secret_key'
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
