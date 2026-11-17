"""#64: a destructive path that reads as one, and a go-live that asks first.

Three rules, each proved through the public interface:

1. Mark for removal is styled apart from the safe action next to it.
2. A Leaving soon row says when it is deleted and what that deletes.
3. Turning dry run off asks for a confirmation that lists what is now due,
   and nothing is stored until the owner confirms, exactly like resuming the
   kill switch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
import json
from pathlib import Path
import re

import pytest

from almali_library_curator.config import load_settings
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.web.format import human_size
from lifecycle_helpers import (
    MOVIE_SIZE, make_candidate, seed_episode, seed_item, seed_library,
)
from test_views import displayed, snapshot


ROOT = Path(__file__).resolve().parents[2]
CATALOGS = {
    locale: json.loads((ROOT / f'almali_library_curator/i18n/{locale}.json').read_text())
    for locale in ('en', 'ca', 'es')
}

FORM_PATTERN = re.compile(r'<form method="post" action="([^"]+)">(.*?)</form>', re.S)
BUTTON_PATTERN = re.compile(r'<button class="([^"]*)" type="submit">(.*?)</button>', re.S)


def catalogs_text(locale: str, key: str, **params) -> str:
    return CATALOGS[locale][key].format(**params)


def marked_item(conn, settings, *, kind='movie'):
    """A real removal in Leaving soon, with a due date the test owns."""
    library = seed_library(conn, '1', 'Movies' if kind == 'movie' else 'Shows',
                           section_type='movie' if kind == 'movie' else 'show')
    if kind == 'movie':
        item = seed_item(conn, library, '11', 'Tenet', rating=7.0)
        candidate = make_candidate(item, '11', 'Tenet')
    else:
        item = seed_item(conn, library, '20', 'Foundation', item_type='series')
        seed_episode(conn, '20', 1, 1)
        seed_episode(conn, '20', 2, 1)
        seed_episode(conn, '20', 2, 2)
        candidate = make_candidate(item, '20', 'Foundation S2', unit=Unit.SEASONS, seasons=(2,))
    engine = LifecycleEngine(conn, settings=settings, clock=lambda: datetime.now(timezone.utc))
    engine.mark_for_removal(candidate, marked_by='owner')
    due = datetime.now(timezone.utc) + timedelta(days=10)
    conn.execute('UPDATE removal_requests SET due_at=?', (due.isoformat(),))
    conn.commit()
    return item, due


def leaving_body(client, locale='en'):
    return unescape(client.get(f'/?tab=leaving_soon&lang={locale}').get_data(as_text=True))


def buttons(html: str) -> list[tuple[str, str]]:
    """Every POST button of a page: its classes and its words."""
    return [(classes, ' '.join(re.sub('<[^>]+>', ' ', body).split()))
            for classes, body in BUTTON_PATTERN.findall(html)]


def settings_form(html: str) -> tuple[str, str]:
    """The settings form's action and its rendered HTML."""
    match = re.search(r'<form class="settings-form" method="post" action="([^"]+)">(.*?)</form>',
                      html, re.S)
    assert match, html[:800]
    return unescape(match.group(1)), match.group(2)


def flashes(client):
    with client.session_transaction() as session:
        return [key for _, key in session.get('_flashes', [])]


# -- rule 1: the destructive action is styled apart --------------------------

def test_mark_for_removal_is_styled_apart_from_the_safe_action(app, client, conn):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet', rating=7.0)
    snapshot(app, [displayed(item, 'Tenet', MOVIE_SIZE)])

    html = client.get('/?tab=candidates').get_data(as_text=True)

    row = re.search(r'<article class="candidate-row card".*?</article>', html, re.S).group(0)
    mark = next(classes for classes, words in buttons(row) if words == 'Mark for removal')
    safe = next(classes for classes, words in buttons(row) if words == 'Keep forever')
    assert 'btn-danger' in mark.split()
    assert 'btn-danger' not in safe.split()
    # A danger action carries a warning icon too: never colour alone.
    assert re.search(r'btn-danger[^>]*><svg[^>]*>.*?#warning', row, re.S)


def test_the_danger_variant_is_a_stylesheet_rule_with_its_own_token():
    css = (ROOT / 'almali_library_curator/static/css/hud.css').read_text()

    assert '--danger' in css and '.btn-danger' in css
    rule = re.search(r'\.btn-danger[^{]*\{([^}]*)\}', css).group(1)
    assert 'background' in rule and 'border-color' in rule


# -- rule 2: the deletion date and what it deletes ---------------------------

@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_a_leaving_row_states_the_deletion_date_the_files_and_the_size(
        app, client, conn, locale):
    _, due = marked_item(conn, app.config['SETTINGS'])

    body = leaving_body(client, locale)

    expected = catalogs_text(
        locale, 'leaving.deletes_on.one',
        date=text.day_month_year(locale, due.astimezone()),
        count=1, size=human_size(MOVIE_SIZE, locale))
    assert expected in body
    if locale == 'en':
        # The English sentence is pinned: date, one file, the size.
        assert re.search(r'Deletes on \d{1,2} \w+ \d{4} · 1 file · \d+ GB', body)


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_a_series_row_counts_only_the_episodes_of_its_own_seasons(
        app, client, conn, locale):
    _, due = marked_item(conn, app.config['SETTINGS'], kind='seasons')

    body = leaving_body(client, locale)

    size = human_size(4 * 1024**3, locale)  # two episodes, 2 GiB each
    expected = catalogs_text(
        locale, 'leaving.deletes_on.other',
        date=text.day_month_year(locale, due.astimezone()),
        count=2, size=size)
    assert expected in body
    # Season 1's episode is out of scope, so it is not counted.
    should_not = catalogs_text(
        locale, 'leaving.deletes_on.other',
        date=text.day_month_year(locale, due.astimezone()),
        count=3, size=size)
    assert should_not not in body


# -- rule 3: turning dry run off asks first, with a summary ------------------

def test_turning_dry_run_off_asks_first_and_lists_what_is_now_due(app, client, conn):
    marked_item(conn, app.config['SETTINGS'])
    settings_file = app.config['SETTINGS'].config_dir / 'settings.json'
    before = settings_file.read_bytes()

    response = client.post('/settings', data={'dry_run': 'false', 'grace_days': '30'})

    assert response.status_code == 200
    assert settings_file.read_bytes() == before, 'nothing may be stored before the confirmation'
    html = response.get_data(as_text=True)
    confirm_action, form = settings_form(html)
    assert confirm_action.split('?')[0] == '/settings/dry-run/confirm'
    panel = re.search(r'<section class="danger-panel[^"]*"[^>]*>(.*?)</section>', html, re.S)
    assert panel, html[:800]
    body = unescape(panel.group(1))
    assert catalogs_text('en', 'settings.dry_run_off.title') in body
    assert catalogs_text('en', 'settings.dry_run_off.items.one') in body
    assert catalogs_text('en', 'settings.dry_run_off.size', size=human_size(MOVIE_SIZE, 'en')) in body
    assert re.search(r'The first deletion is due on \d{1,2} \w+ \d{4}', body)
    assert catalogs_text('en', 'settings.dry_run_off.kill_switch_off') in body
    # The step that saves is the danger action, and the way out saves nothing.
    assert any('btn-danger' in classes.split()
               and words == catalogs_text('en', 'settings.dry_run_off.confirm')
               for classes, words in buttons(panel.group(0)))
    assert 'href="/settings?lang=en"' in body
    assert catalogs_text('en', 'settings.dry_run_off.cancel') in body
    # Every edited field came back with the confirmation, dry run included.
    assert 'name="dry_run"' in form and 'name="grace_days"' in form


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_the_go_live_confirmation_is_translated(app, client, conn, locale):
    marked_item(conn, app.config['SETTINGS'])

    html = unescape(client.post(f'/settings?lang={locale}',
                                data={'dry_run': 'false'}).get_data(as_text=True))

    for key in ('settings.dry_run_off.title', 'settings.dry_run_off.message',
                'settings.dry_run_off.confirm', 'settings.dry_run_off.cancel'):
        assert CATALOGS[locale][key] in html, key


def test_confirming_turns_deletions_live_and_saves_every_edited_field(app, client, conn):
    marked_item(conn, app.config['SETTINGS'])
    pending = {'dry_run': 'false', 'grace_days': '30', 'scan_time': '07:45'}
    html = client.post('/settings', data=pending).get_data(as_text=True)
    confirm_action, _ = settings_form(html)
    assert load_settings(app.config['SETTINGS'].config_dir).dry_run is True

    response = client.post(confirm_action, data=pending)

    assert response.status_code == 303
    assert response.headers['Location'] == '/settings?lang=en'
    assert flashes(client) == ['web.flash.dry_run_off']
    stored = load_settings(app.config['SETTINGS'].config_dir)
    assert stored.dry_run is False and stored.grace_days == 30 and stored.scan_time == '07:45'


def test_the_confirmation_says_so_when_nothing_is_due(app, client, conn):
    response = client.post('/settings', data={'dry_run': 'false'})

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert catalogs_text('en', 'settings.dry_run_off.none') in html
    assert 'The first deletion is due on' not in html


def test_only_the_change_from_dry_run_to_live_asks(app, client, conn):
    # Turning it on, or saving anything else, never asks.
    assert client.post('/settings', data={'dry_run': 'true'}).status_code == 303
    asking = client.post('/settings', data={'dry_run': 'false'})
    assert asking.status_code == 200
    confirm_action, _ = settings_form(asking.get_data(as_text=True))
    assert client.post(confirm_action, data={'dry_run': 'false'}).status_code == 303
    # Once live, saving again is an ordinary save.
    assert client.post('/settings', data={'dry_run': 'false'}).status_code == 303


def test_a_refused_confirmation_saves_nothing_and_rerenders_the_errors(app, client, conn):
    marked_item(conn, app.config['SETTINGS'])
    settings_file = app.config['SETTINGS'].config_dir / 'settings.json'
    before = settings_file.read_bytes()

    response = client.post('/settings/dry-run/confirm',
                           data={'dry_run': 'false', 'grace_days': '2'})

    assert response.status_code == 400
    assert settings_file.read_bytes() == before
    assert load_settings(app.config['SETTINGS'].config_dir).dry_run is True


# -- the neighbour that already confirmed still does, with the danger action --

def test_resuming_the_kill_switch_still_confirms_with_the_danger_action(app, client, conn):
    assert client.post('/settings/kill-switch', data={'enabled': 'on'}).status_code == 303

    response = client.post('/settings/kill-switch', data={'enabled': 'off'})

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="confirm-heading"' in html
    assert any('btn-danger' in classes.split() and words == 'Resume deletions'
               for classes, words in buttons(html))
    assert client.post('/settings/kill-switch/confirm').status_code == 303


def test_a_confirmation_posted_with_dry_run_back_on_stays_live_free(app, client, conn):
    """The panel's form is the owner's until they submit it: flipping the
    select back to Yes saves it, and the message never claims deletions ran."""
    html = client.post('/settings', data={'dry_run': 'false'}).get_data(as_text=True)
    confirm_action, _ = settings_form(html)

    response = client.post(confirm_action, data={'dry_run': 'true'})

    assert response.status_code == 303
    assert flashes(client) == ['web.flash.settings_saved']
    assert load_settings(app.config['SETTINGS'].config_dir).dry_run is True


# -- the guard holds on every save path, whatever else is posted --------------

def test_the_wizard_cannot_arm_the_deletions_without_the_confirmation(app, client, conn):
    """The wizard's library step has no dry-run control: posting one changes nothing."""
    seed_library(conn, '1', 'Movies')

    response = client.post('/settings', data={'wizard_step': 'libraries',
                                              'curated_libraries': '1',
                                              'dry_run': 'false'})

    assert response.status_code == 303
    assert response.headers['Location'] == '/setup?lang=en&step=connections'
    stored = load_settings(app.config['SETTINGS'].config_dir)
    assert stored.dry_run is True, 'nothing may arm the deletions without the panel'
    assert stored.curated_libraries == ('1',)


def test_the_wizard_still_saves_its_libraries_and_continues(app, client, conn):
    seed_library(conn, '1', 'Movies')

    response = client.post('/settings', data={'wizard_step': 'libraries',
                                              'curated_libraries': '1',
                                              'protected_libraries': ''})

    assert response.status_code == 303
    assert response.headers['Location'] == '/setup?lang=en&step=connections'
    stored = load_settings(app.config['SETTINGS'].config_dir)
    assert stored.curated_libraries == ('1',) and stored.dry_run is True


def test_a_forged_wizard_step_does_not_bypass_the_confirmation(app, client, conn):
    """Any other wizard_step falls through to the ordinary save, guard included."""
    settings_file = app.config['SETTINGS'].config_dir / 'settings.json'
    before = settings_file.read_bytes()

    response = client.post('/settings', data={'wizard_step': 'plex', 'dry_run': 'false'})

    assert response.status_code == 200
    settings_form(response.get_data(as_text=True))  # the panel, not a redirect
    assert settings_file.read_bytes() == before


def test_the_confirmation_route_is_the_only_one_that_stores_live_deletions(app, client, conn):
    """Every non-confirming path leaves dry run on; the confirming one stores it."""
    for path, data in (('/settings', {'dry_run': 'false'}),
                       ('/settings', {'dry_run': 'false', 'wizard_step': 'libraries'}),
                       ('/settings', {'dry_run': 'false', 'wizard_step': 'anything'})):
        assert client.post(path, data=data).status_code in (200, 303)
        assert load_settings(app.config['SETTINGS'].config_dir).dry_run is True, (path, data)

    assert client.post('/settings/dry-run/confirm', data={'dry_run': 'false'}).status_code == 303
    assert load_settings(app.config['SETTINGS'].config_dir).dry_run is False
