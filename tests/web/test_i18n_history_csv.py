"""S1 (#73): History dates and CSV headers follow the locale, and the
language switch offers a GET-safe target on every re-rendered POST page."""

from __future__ import annotations

import csv
import io
import re

import pytest

from almali_library_curator.i18n import SUPPORTED_LOCALES, translate

OCCURRED_AT = '2026-11-01T12:00:00+00:00'

# The localised History date per locale; the ISO slice must never leak.
LOCALISED_DATE = {
    'en': '1 November 2026',
    'ca': "1 de novembre 2026",
    'es': '1 de noviembre 2026',
}


def seed_event(conn):
    conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,"
        " disk, reason, marked_by, occurred_at) VALUES ('deleted', 'Film', 'movie',"
        " 100, 0, 'disk3', 'A reason', 'owner', ?)",
        (OCCURRED_AT,),
    )
    conn.commit()


@pytest.mark.parametrize('locale', SUPPORTED_LOCALES)
def test_history_dates_are_localised_not_the_iso_slice(client, conn, locale):
    seed_event(conn)

    page = client.get(f'/history?lang={locale}').get_data(as_text=True)

    assert LOCALISED_DATE[locale] in page, locale
    assert '2026-11-01' not in page, locale


CSV_HEADER = {
    'en': [field for field in ('id', 'outcome', 'title', 'unit', 'category', 'reason',
                               'size_bytes', 'freed_bytes', 'disk', 'marked_by',
                               'occurred_at', 'error')],
    'ca': ['id', 'resultat', 'títol', 'tipus', 'categoria', 'motiu', 'mida_bytes',
           'alliberat_bytes', 'disc', 'marcat_per', 'data', 'error'],
    'es': ['id', 'resultado', 'título', 'tipo', 'categoría', 'motivo', 'tamaño_bytes',
           'liberado_bytes', 'disco', 'marcado_por', 'fecha', 'error'],
}


@pytest.mark.parametrize('locale', SUPPORTED_LOCALES)
def test_csv_header_row_is_translated_for_the_active_locale(client, conn, locale):
    seed_event(conn)

    response = client.get(f'/history.csv?lang={locale}')

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True), newline='')))
    assert rows[0] == CSV_HEADER[locale]
    # The row values keep their own shape: only the header is localised.
    assert rows[1][-1] == '' and rows[1][10] == OCCURRED_AT


def test_the_english_csv_header_keeps_the_published_field_names():
    """The English catalog is the source of the CSV contract (test_csv_escaping)."""
    assert [translate(f'csv.event.{name}', 'en') for name in CSV_HEADER['en']] == CSV_HEADER['en']


def locale_links(page):
    nav = re.search(r'<nav class="locale-switcher".*?</nav>', page, re.S)
    assert nav is not None
    return re.findall(r'href="([^"]+)"[^>]*hreflang="(\w\w)"', nav.group(0))


# POST-only routes whose response is a rendered page (validation error and
# confirmation), so the switcher used to GET a URL that answers 405.
POST_RERENDERS = [
    ('/settings', {'grace_days': '2'}, 400),
    ('/connections/radarr', {'radarr_base_url': 'http://radarr.invalid'}, 400),
    ('/settings/kill-switch', {'enabled': 'false'}, 200),
]


@pytest.mark.parametrize('path,data,status', POST_RERENDERS)
def test_language_links_answer_200_after_a_post_rerender(client, path, data, status):
    response = client.post(path, data=data)
    assert response.status_code == status

    links = locale_links(response.get_data(as_text=True))
    assert [locale for _, locale in links] == list(SUPPORTED_LOCALES)
    for href, locale in links:
        assert 'lang=' + locale in href, (href, locale)
        assert client.get(href).status_code == 200, (href, locale)


def test_language_links_keep_the_current_page_after_a_get(client):
    response = client.get('/history')

    for href, locale in locale_links(response.get_data(as_text=True)):
        assert href.startswith('/history?'), href
        assert client.get(href).status_code == 200
