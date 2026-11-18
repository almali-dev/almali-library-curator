"""Flash outcome semantics on public action routes."""
import re

import pytest

from almali_library_curator.config import load_settings


def flashed(client):
    with client.session_transaction() as session:
        return session.get('_flashes', [])


def flash_markup(response, icon, role):
    return re.search(
        rf'<div class="flash-message" role="{role}">\s*'
        rf'<svg.*?<use href="[^"]*icons\.svg(?:\?v=[0-9a-f]+)?#{icon}"></use>',
        response.get_data(as_text=True),
        re.S,
    )


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_successful_action_flashes_success_with_a_polite_check(app, client, locale):
    response = client.post(f'/settings?lang={locale}', data={'grace_days': '31'})

    assert response.status_code == 303
    assert flashed(client) == [('success', 'web.flash.settings_saved')]
    assert load_settings(app.config['SETTINGS'].config_dir, env={}).grace_days == 31
    rendered = client.get(response.headers['Location'])
    assert rendered.status_code == 200
    assert flash_markup(rendered, 'check', 'status')


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_failed_actions_flash_error_with_an_assertive_warning(app, client, locale):
    class BrokenRunner:
        def start_background(self):
            raise RuntimeError('scan did not start')

    app.extensions['scan_runner'] = BrokenRunner()

    response = client.post(f'/items/999/mark?lang={locale}', data={'unit': 'movie'})
    assert response.status_code == 303
    assert flashed(client) == [('error', 'web.flash.invalid_scope')]
    rendered = client.get(response.headers['Location'])
    assert rendered.status_code == 200
    assert flash_markup(rendered, 'warning', 'alert')

    response = client.post(f'/actions/scan?lang={locale}')
    assert response.status_code == 303
    assert flashed(client) == [('error', 'web.flash.action_failed')]
    rendered = client.get(response.headers['Location'])
    assert rendered.status_code == 200
    assert flash_markup(rendered, 'warning', 'alert')
