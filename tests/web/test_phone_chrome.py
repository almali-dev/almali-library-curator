"""#65: below 992 px the secondary chrome folds away and the list comes first.

The phone layout keeps every control and every link, but the brand bar, the
navigation, the sources panel, the language links, the KPI cards and the scan
steps fold into disclosures that are open on desktop and closed on a phone.
These tests read the rendered HTML, the stylesheet and the served assets; the
positions are measured in Chromium in tests/web/test_browser_layout.py.
"""
from __future__ import annotations

from html import unescape
import json
from pathlib import Path
import re

import pytest

from test_views import displayed, snapshot

ROOT = Path(__file__).resolve().parents[2]
CATALOGS = {
    locale: json.loads((ROOT / f'almali_library_curator/i18n/{locale}.json').read_text())
    for locale in ('en', 'ca', 'es')
}
CSS = (ROOT / 'almali_library_curator/static/css/hud.css').read_text()
LOCALES = ('en', 'ca', 'es')


def page(client, path='/', locale='en'):
    return unescape(client.get(f'{path}?lang={locale}').get_data(as_text=True))


def disclosure(html: str, marker: str) -> str:
    """The <details> element whose class list contains ``marker``."""
    for match in re.finditer(r'<details class="([^"]*)"([^>]*)>', html):
        if marker in match.group(1).split():
            return html[match.start():html.index('</details>', match.start())]
    raise AssertionError(f'no disclosure {marker}')


def summary_of(element: str) -> str:
    inner = re.search(r'<summary[^>]*>(.*?)</summary>', element, re.S)
    assert inner, element[:200]
    return ' '.join(re.sub('<[^>]+>', ' ', inner.group(1)).split())


# -- the shell folds its secondary chrome ------------------------------------

def test_the_navigation_and_the_sources_fold_into_one_menu(app, client):
    html = page(client)

    menu = disclosure(html, 'menu-disclosure')
    assert 'data-collapse-small' in menu.split('>')[0], 'the phone rule needs to find it'
    assert re.search(r'\sopen(?:[ >])', menu.split('>')[0] + '>'), 'open without JavaScript'
    assert '<nav class="main-nav"' in menu
    assert '<section class="sources-panel"' in menu
    # The brand stays outside: it is the way home, not chrome.
    assert html.index('class="brand"') < html.index('menu-disclosure')


@pytest.mark.parametrize('locale', LOCALES)
def test_the_language_links_fold_behind_a_translated_summary(app, client, locale):
    html = page(client, locale=locale)

    panel = disclosure(html, 'locale-disclosure')
    assert summary_of(panel) == CATALOGS[locale]['locale.label']
    # The switcher itself is unchanged, so its links keep their names.
    assert '<nav class="locale-switcher" aria-label="' in panel
    assert panel.count('hreflang=') == 3


def test_the_kpi_cards_fold_into_a_summary_disclosure(app, client):
    html = page(client)

    summary = disclosure(html, 'summary-disclosure')
    assert '<section class="summary-grid"' in summary
    assert html.count('summary-card') == 4
    # The tabs are a sibling of the disclosure, not inside the folded part.
    order = re.search(r'<div class="library-order">(.*?)</div>\s*<p class="safety-status"',
                      html, re.S)
    assert order, html[:600]
    assert 'summary-disclosure' in order.group(1) and 'class="tab-bar"' in order.group(1)


@pytest.mark.parametrize('locale', LOCALES)
def test_the_scan_steps_fold_behind_a_translated_summary(app, client, locale):
    snapshot(app, [displayed(1, 'Tenet', 10)])
    html = page(client, locale=locale)

    panel = disclosure(html, 'scan-steps-disclosure')
    assert summary_of(panel) == CATALOGS[locale]['scan.steps']
    assert '<ul class="scan-steps">' in panel
    # The outcome line itself stays in view: it is the answer, not the detail.
    assert re.search(r'<p class="scan-state">', html)
    assert html.index('class="scan-state"') < html.index('scan-steps-disclosure')


def test_every_new_label_is_translated_in_every_locale(app, client):
    for locale in LOCALES:
        html = page(client, locale=locale)
        assert CATALOGS[locale]['nav.menu'] in summary_of(disclosure(html, 'menu-disclosure'))
        assert CATALOGS[locale]['summary.label'] in summary_of(disclosure(html, 'summary-disclosure'))


# -- the rules that only apply below 992 px ----------------------------------

def phone_rules() -> str:
    match = re.search(r'@media \(max-width: 991\.98px\) \{(.*?)\n\}', CSS, re.S)
    assert match, 'the phone block is missing'
    return match.group(1)


def test_the_phone_rules_put_the_tabs_first_and_the_kpis_side_by_side():
    rules = phone_rules()

    assert 'display: flex' in rules and 'flex-direction: column' in rules
    assert re.search(r'\.library-order[^{]*\.tab-bar[^{]*\{[^}]*order: -1', rules, re.S)
    assert re.search(r'\.summary-grid[^{]*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)', rules, re.S)


def test_the_disclosure_summary_is_hidden_from_desktop():
    desktop = re.search(r'@media \(min-width: 992px\) \{(.*?)\n\}', CSS, re.S).group(1)

    assert re.search(r'\.chrome-disclosure[^{]*\{[^}]*display: none', desktop, re.S)


def test_the_category_filters_stay_one_row_on_a_phone():
    rules = phone_rules()

    assert re.search(r'\.category-filters[^{]*\{[^}]*flex-wrap: nowrap[^}]*overflow-x: auto',
                     rules, re.S)


# -- the folding is a served script, never an inline one ---------------------

def test_the_folding_script_is_served_and_referenced(app, client):
    script = client.get('/static/js/chrome-disclosures.js')

    assert script.status_code == 200
    body = script.get_data(as_text=True)
    assert 'data-collapse-small' in body and 'matchMedia' in body
    html = page(client)
    assert re.search(r'<script src="[^"]*js/chrome-disclosures\.js(?:\?v=[0-9a-f]+)?"[^>]*defer', html)


def test_the_pages_carry_no_inline_script(app, client):
    html = page(client)

    inline = [tag for tag in re.findall(r'<script[^>]*>', html) if 'src=' not in tag]
    assert inline == []


def test_a_screen_without_navigation_carries_no_empty_menu(app):
    """The login and error screens empty the sidebar: no Menu panel with nothing in it."""
    anonymous = app.test_client()
    for path in ('/login', '/no-such-screen'):
        html = unescape(anonymous.get(path).get_data(as_text=True))
        assert 'menu-disclosure' not in html, path


# -- #113: About and Sign out leave the phone top bar for the Menu -----------

def top_bar(html: str) -> str:
    """The <header> slice, where the signed-in actions live."""
    start = html.index('<header class="page-header">')
    return html[start:html.index('</header>', start)]


@pytest.mark.parametrize('locale', LOCALES)
def test_the_phone_menu_carries_the_about_link_and_the_sign_out_form(app, client, locale):
    """S2 (#113): both are one tap away in the Menu, and Sign out stays a POST."""
    html = page(client, locale=locale)
    menu = disclosure(html, 'menu-disclosure')

    assert re.search(rf'<a class="btn" href="/about\?lang={locale}">', menu), 'About is in the phone Menu'
    assert CATALOGS[locale]['nav.about'] in menu

    form = re.search(r'<form method="post" action="/logout[^"]*">(.*?)</form>', menu, re.S)
    assert form, 'the phone Menu carries the Sign out form'
    assert 'name="csrf_token"' in form.group(1), 'Sign out stays a POST with its CSRF token'
    assert CATALOGS[locale]['auth.logout'] in form.group(1)


def test_the_phone_rules_hide_the_top_bar_account_actions():
    """S2 (#113): the phone Menu replaces the top-bar copy, it does not add to it."""
    rules = phone_rules()

    assert re.search(r'\.header-account[^{]*\{[^}]*display: none', rules, re.S)
    assert re.search(r'\.menu-account-actions[^{]*\{[^}]*display: flex', rules, re.S)


def test_the_desktop_top_bar_keeps_about_and_sign_out_in_order(app, client):
    """S2 (#113): desktop is unchanged; the account actions stay in the header."""
    html = page(client)
    header = top_bar(html)

    assert 'class="header-account"' in header
    about = re.search(r'<a class="btn" href="/about\?lang=en">', header)
    scan = re.search(r'<form method="post" action="/actions/scan[^"]*">', header)
    logout = re.search(r'<form method="post" action="/logout[^"]*">(.*?)</form>', header, re.S)
    assert about and scan and logout, header
    assert about.start() < scan.start() < logout.start(), 'the desktop order is unchanged'
    assert 'name="csrf_token"' in logout.group(1)
    assert CATALOGS['en']['nav.about'] in header
    assert CATALOGS['en']['auth.logout'] in header
