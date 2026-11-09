"""T10 gap (b): error pages are translated screens, not Flask's English page.

S7: "UI text in English, Catalan and Spanish from day one"; S9: "every string
through translation keys". A 404, a 405, a 500 and a refused CSRF token render
through the app layout, in the request's locale, with the right status code.
"""
from dataclasses import replace

from markupsafe import Markup
import pytest

from almali_library_curator.app import create_app
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle.safety import kill_switch_enabled

LOCALES = ('en', 'ca', 'es')


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def assert_error_page(response, status, kind, locale):
    html = response.get_data(as_text=True)
    assert response.status_code == status
    assert response.mimetype == 'text/html'
    # The app layout remains, but error pages omit links that may fail again.
    assert f'<html lang="{locale}"' in html
    assert 'data-nav=' not in html and 'class="page-footer"' in html
    body = text(response)
    for key in (f'error.{kind}.title', f'error.{kind}.message', 'error.back'):
        expected = ' '.join(Markup(translate(key, locale)).striptags().split())
        assert expected in body, (key, locale)
    # Never Flask's built-in English page.
    assert 'The requested URL was not found' not in body
    assert 'The method is not allowed' not in body
    assert 'Internal Server Error' not in body or locale == 'en'
    assert 'CSRF' not in body


@pytest.mark.parametrize('locale', LOCALES)
def test_an_unknown_page_answers_a_translated_404(client, locale):
    assert_error_page(client.get(f'/no-such-screen?lang={locale}'), 404, 'not_found', locale)


@pytest.mark.parametrize('locale', LOCALES)
def test_the_locale_also_follows_the_browser_language(client, locale):
    response = client.get('/no-such-screen', headers={'Accept-Language': locale})
    assert_error_page(response, 404, 'not_found', locale)


@pytest.mark.parametrize('locale', LOCALES)
def test_a_wrong_method_answers_a_translated_405_with_the_allowed_methods(client, locale):
    response = client.get(f'/scan?lang={locale}')
    assert_error_page(response, 405, 'method_not_allowed', locale)
    assert 'POST' in response.headers['Allow']


@pytest.mark.parametrize('locale', LOCALES)
def test_an_unexpected_failure_answers_a_translated_500(app, locale):
    app.config['PROPAGATE_EXCEPTIONS'] = False

    @app.get('/boom')
    def boom():
        raise RuntimeError('secret detail that must never reach the page')

    response = app.test_client().get(f'/boom?lang={locale}')
    assert_error_page(response, 500, 'server', locale)
    assert 'secret detail' not in response.get_data(as_text=True)


@pytest.mark.parametrize('locale', LOCALES)
def test_a_refused_csrf_token_keeps_400_changes_nothing_and_is_translated(settings, conn, locale):
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    response = app.test_client().post(f'/settings/kill-switch?lang={locale}', data={'enabled': 'on'})
    assert_error_page(response, 400, 'csrf', locale)
    assert kill_switch_enabled(conn) is False


def test_a_refused_csrf_token_writes_no_file_on_a_fresh_install(settings):
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True)
    before = sorted(settings.config_dir.glob('*'))
    response = app.test_client().post('/settings/kill-switch', data={'enabled': 'on'})
    assert response.status_code == 400
    assert sorted(settings.config_dir.glob('*')) == before


@pytest.mark.parametrize('locale', LOCALES)
def test_a_wrong_method_on_a_screen_action_is_translated_too(client, locale):
    response = client.get(f'/settings/kill-switch?lang={locale}')
    assert_error_page(response, 405, 'method_not_allowed', locale)
    assert 'POST' in response.headers['Allow']


def test_the_json_health_endpoint_keeps_its_own_answer(client):
    assert client.get('/healthz').is_json
