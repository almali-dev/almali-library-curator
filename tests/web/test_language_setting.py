"""S1e (#47): the language is chosen in Settings and through ALC_LOCALE.

The setting follows the rules of every other setting: the value is validated
against the supported locales, saved to the settings file, and ``ALC_LOCALE``
wins over the saved value on every load (environment > file).
"""
from __future__ import annotations

import json
import re

import pytest

from almali_library_curator.config import load_settings
from almali_library_curator.i18n import SUPPORTED_LOCALES, translate


def locale_select(html: str) -> str:
    match = re.search(r'<select name="locale".*?</select>', html, re.S)
    assert match is not None, "the settings screen carries no language select"
    return match.group(0)


def test_the_settings_screen_offers_every_supported_locale(client):
    html = client.get('/settings').get_data(as_text=True)
    select = locale_select(html)
    assert [locale for locale in SUPPORTED_LOCALES
            if f'<option value="{locale}"' in select] == list(SUPPORTED_LOCALES)
    for locale in SUPPORTED_LOCALES:
        assert translate(f'locale.{locale}', 'en') in select
    assert re.search(r'<option value="en"[^>]*selected', select)


def test_saving_catalan_persists_it_and_renders_the_default_request_in_catalan(client, app):
    response = client.post('/settings', data={'locale': 'ca'})
    assert response.status_code == 303
    saved = json.loads((app.config['SETTINGS'].config_dir / 'settings.json').read_text())
    assert saved['locale'] == 'ca'
    # No ?lang= and no Accept-Language: the saved setting decides.
    page = client.get('/settings').get_data(as_text=True)
    assert '<html lang="ca"' in page
    assert translate('nav.settings', 'ca') in page
    # ?lang= still wins over the saved setting.
    spanish = client.get('/settings?lang=es').get_data(as_text=True)
    assert '<html lang="es"' in spanish
    assert translate('nav.settings', 'es') in spanish
    # Accept-Language still wins over the saved setting.
    accepted = client.get('/settings', headers={'Accept-Language': 'es'}).get_data(as_text=True)
    assert '<html lang="es"' in accepted
    # An unsupported ?lang= falls back to the saved setting.
    assert '<html lang="ca"' in client.get('/settings?lang=fr').get_data(as_text=True)


def test_the_environment_variable_wins_over_the_saved_choice(client, monkeypatch):
    assert client.post('/settings', data={'locale': 'ca'}).status_code == 303
    monkeypatch.setenv('ALC_LOCALE', 'es')
    page = client.get('/settings').get_data(as_text=True)
    assert '<html lang="es"' in page


def test_an_unsupported_locale_is_refused_with_the_error_summary(client, app):
    path = app.config['SETTINGS'].config_dir / 'settings.json'
    before = path.read_bytes()
    response = client.post('/settings', data={'locale': 'fr'})
    assert response.status_code == 400
    assert path.read_bytes() == before
    html = response.get_data(as_text=True)
    summary = re.search(r'<div class="error-summary" role="alert">(.*?)</div>', html, re.S)
    assert summary is not None
    assert 'href="#field-locale"' in summary.group(1)
    assert translate('web.form.locale_unsupported', 'en') in summary.group(1)


def test_the_saved_locale_is_loaded_and_the_environment_wins(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'locale': 'ca'}))
    assert load_settings(tmp_path, env={}).locale == 'ca'
    assert load_settings(tmp_path, env={'ALC_LOCALE': 'es'}).locale == 'es'


@pytest.mark.parametrize('env', [{'ALC_LOCALE': 'fr'}, {'ALC_LOCALE': 'EN'},
                                 {'ALC_LOCALE': 'fr-FR'}])
def test_an_unsupported_environment_locale_is_rejected(tmp_path, env):
    with pytest.raises(ValueError, match='locale'):
        load_settings(tmp_path, env=env)
    assert not (tmp_path / 'settings.json').exists()


def test_an_unsupported_stored_locale_is_rejected(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'locale': 'fr'}))
    with pytest.raises(ValueError, match='locale'):
        load_settings(tmp_path, env={})
