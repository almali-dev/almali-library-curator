"""#134: the Candidates, Protected and Leaving soon lists sort on request.

Every test drives the real screen over a real migrated database. The three
lists answer four orders -- size (largest first), age (added longest ago),
rating (lowest first) and time without plays (longest first) -- and each
list keeps its own current order as the default: Candidates and Protected
by size, Leaving soon by the soonest deletion date. The choice travels in
the ``sort`` query parameter, so a refresh, a shared link, the pager, a
category filter, a tab and a row action all rebuild the same ordered list.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from almali_library_curator.i18n import load_catalogs, translate


NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000

# Four rows that disagree on every order, plus one missing value per order:
# no added date (Delta), no rating (Gamma) and no play (Gamma, Delta).
ITEMS = (
    # title, size in GB, added_at, rating, last_played_at
    ('Alpha', 30, '2020-01-01T12:00:00+00:00', 9.0, '2026-11-01T12:00:00+00:00'),
    ('Beta', 10, '2024-06-01T12:00:00+00:00', 3.0, '2023-01-01T12:00:00+00:00'),
    ('Gamma', 20, '2022-03-01T12:00:00+00:00', None, None),
    ('Delta', 40, None, 6.0, None),
)

EXPECTED = {
    'size': ('Delta', 'Alpha', 'Gamma', 'Beta'),
    'age': ('Alpha', 'Gamma', 'Beta', 'Delta'),
    'rating': ('Beta', 'Delta', 'Alpha', 'Gamma'),
    # Never played first, then by the reference the detection engine uses
    # (the last play, else the added date): Delta has neither, so it leads.
    'unplayed': ('Delta', 'Gamma', 'Beta', 'Alpha'),
    # Leaving soon's own current order: the soonest deletion date first.
    'due': ('Delta', 'Beta', 'Gamma', 'Alpha'),
}

SORT_KEYS = ('size', 'age', 'rating', 'unplayed')
LEAVING_SORT_KEYS = ('due',) + SORT_KEYS

SORT_NAV = re.compile(r'<nav class="category-filters sort-filters"[^>]*>(.*?)</nav>', re.S)
ACTIVE_SORT = re.compile(r'<a class="btn filter-button active" href="([^"]+)"'
                         r'[^>]*aria-current="page"')
TITLE = re.compile(r'<h3 class="candidate-title">([^<]+)</h3>')
DUE_OFFSET_DAYS = {'Alpha': 10, 'Beta': 3, 'Gamma': 7, 'Delta': 1}


# -- seeding the rows the screen reads ---------------------------------------

def seed_library(conn) -> int:
    cursor = conn.execute(
        "INSERT INTO libraries (plex_key, title, section_type, protected)"
        " VALUES ('1', 'Movies', 'movie', 0)")
    conn.commit()
    return int(cursor.lastrowid)


def seed_items(conn) -> dict[str, int]:
    """The four media items, with the dates and ratings the sort reads."""
    library = seed_library(conn)
    identifiers = {}
    for title, size, added, rating, played in ITEMS:
        cursor = conn.execute(
            'INSERT INTO media_items (library_id, plex_rating_key, title, item_type,'
            ' added_at, last_played_at, rating, rating_source, file_size_bytes, disk)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (library, f'key-{title}', title, 'movie', added, played, rating,
             'Plex' if rating is not None else None, size * GB, 'disk1'))
        identifiers[title] = int(cursor.lastrowid)
    conn.commit()
    return identifiers


def display_row(identifier, title, size, rating, played, *, protected=False) -> dict:
    categories = ['stalled_series'] if title == 'Beta' else ['never_watched']
    row = dict(media_item_id=identifier, title=title, unit='movie', seasons=[],
               type_badge='MOVIE', categories=categories, reasons=['A reason.'],
               reason='A reason.', size_bytes=size * GB, disk='disk1', rating=rating,
               rating_source='Plex' if rating is not None else None,
               last_played_at=played)
    if protected:
        row.update(protection_kind='keep_forever', protection_reason='Kept for ever.',
                   protection_reason_parts=[])
    return row


def publish(conn, protected=False):
    """Publish the four rows through the scan cache the screen reads."""
    identifiers = seed_items(conn)
    rows = [display_row(identifiers[title], title, size, rating, played,
                        protected=protected)
            for title, size, _added, rating, played in ITEMS]
    report = json.dumps({
        'sync': {'status': 'finished'}, 'enrichment': {'status': 'finished'},
        'detection': {'status': 'finished', 'counts': {
            'candidates': 0 if protected else len(rows),
            'protected': len(rows) if protected else 0,
            'candidate_rows': [] if protected else rows,
            'protected_rows': rows if protected else []}},
        'recheck': {'status': 'finished'}, 'deletion': {'status': 'finished'},
    })
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger,"
        " candidates_found, report) VALUES (?, ?, 'finished', 'dry_run', 'manual', ?, ?)",
        (NOW.isoformat(), NOW.isoformat(), 0 if protected else len(rows), report))
    conn.commit()
    return identifiers


def open_request(conn, media_item_id: int, days: int) -> None:
    """One open removal, due ``days`` from the frozen now.

    The scope carrier is the candidates row the lifecycle engine reads, so
    the Leaving soon screen builds the row exactly as a marked one.
    """
    facts = json.dumps({'unit': 'movie', 'seasons': []})
    candidate = conn.execute(
        "INSERT INTO candidates (media_item_id, category, reason, facts)"
        " VALUES (?, 'never_watched', 'A reason.', ?)", (media_item_id, facts)).lastrowid
    due = (NOW + timedelta(days=days)).isoformat()
    conn.execute(
        'INSERT INTO removal_requests (media_item_id, candidate_id, category, reason,'
        ' grace_days, due_at, marked_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (media_item_id, candidate, 'never_watched', 'A reason.', 14, due, NOW.isoformat()))
    conn.commit()


def publish_leaving(conn) -> dict[str, int]:
    identifiers = seed_items(conn)
    for title, media_item_id in identifiers.items():
        open_request(conn, media_item_id, DUE_OFFSET_DAYS[title])
    return identifiers


# -- reading the rendered page -----------------------------------------------

def page(client, **params):
    query = '&'.join(f'{name}={value}' for name, value in params.items())
    return client.get('/?' + query)


def titles(html: str) -> list[str]:
    return TITLE.findall(html)


def sort_nav(html: str) -> str:
    match = SORT_NAV.search(html)
    assert match, html[:400]
    return match.group(1)


def sort_hrefs(html: str) -> dict[str, str]:
    """The sort value each control of the sort bar links to."""
    hrefs = {}
    for href in re.findall(r'<a class="btn filter-button[^"]*" href="([^"]+)"', sort_nav(html)):
        query = parse_qs(urlsplit(href.replace('&amp;', '&')).query)
        hrefs[query['sort'][0]] = href.replace('&amp;', '&')
    return hrefs


def active_sort(html: str) -> str:
    match = ACTIVE_SORT.search(sort_nav(html))
    assert match, sort_nav(html)
    query = parse_qs(urlsplit(match.group(1).replace('&amp;', '&')).query)
    return query['sort'][0]


# -- one order per choice, on every list -------------------------------------

@pytest.mark.parametrize('sort', SORT_KEYS)
def test_each_sort_key_orders_the_candidates_list(app, conn, client, sort):
    publish(conn)

    response = page(client, tab='candidates', sort=sort)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == list(EXPECTED[sort])
    assert active_sort(html) == sort


@pytest.mark.parametrize('sort', SORT_KEYS)
def test_each_sort_key_orders_the_protected_list(app, conn, client, sort):
    publish(conn, protected=True)

    response = page(client, tab='protected', sort=sort)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == list(EXPECTED[sort])
    assert active_sort(html) == sort


@pytest.mark.parametrize('sort', LEAVING_SORT_KEYS)
def test_each_sort_key_orders_the_leaving_soon_list(app, conn, client, sort):
    publish_leaving(conn)

    response = page(client, tab='leaving_soon', sort=sort)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == list(EXPECTED[sort])
    assert active_sort(html) == sort


# -- the current order stays the default -------------------------------------

@pytest.mark.parametrize('tab,protected', [('candidates', False), ('protected', True)])
def test_candidates_and_protected_stay_largest_first_by_default(app, conn, client, tab, protected):
    publish(conn, protected=protected)

    html = page(client, tab=tab).get_data(as_text=True)

    assert titles(html) == list(EXPECTED['size'])
    assert active_sort(html) == 'size'


def test_leaving_soon_stays_soonest_deletion_first_by_default(app, conn, client):
    publish_leaving(conn)

    html = page(client, tab='leaving_soon').get_data(as_text=True)

    assert titles(html) == list(EXPECTED['due'])
    assert active_sort(html) == 'due'


@pytest.mark.parametrize('tab,protected', [('candidates', False), ('protected', True)])
@pytest.mark.parametrize('sort', ('bogus', 'due', ''))
def test_an_unknown_value_falls_back_to_the_list_default_without_error(
        app, conn, client, tab, protected, sort):
    publish(conn, protected=protected)

    response = page(client, tab=tab, sort=sort)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == list(EXPECTED['size'])
    assert active_sort(html) == 'size'


def test_an_unknown_leaving_soon_value_falls_back_to_its_default(app, conn, client):
    publish_leaving(conn)

    response = page(client, tab='leaving_soon', sort='bogus')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == list(EXPECTED['due'])
    assert active_sort(html) == 'due'


# -- the choice travels with the screen --------------------------------------

def test_the_tab_links_carry_the_chosen_sort(app, conn, client):
    publish(conn, protected=True)

    html = page(client, tab='candidates', sort='rating').get_data(as_text=True)

    tabs = re.findall(r'<a class="tab-link[^"]*" href="([^"]+)"', html)
    assert len(tabs) == 3
    for href in tabs:
        assert 'sort=rating' in href.replace('&amp;', '&'), href


def test_the_category_filter_keeps_the_chosen_sort(app, conn, client):
    publish(conn)

    html = page(client, tab='candidates', category='never_watched',
                sort='age').get_data(as_text=True)

    # The filter still selects its own rows, in the order the owner asked
    # for, and its link keeps that order.
    assert titles(html) == ['Alpha', 'Gamma', 'Delta']
    assert active_sort(html) == 'age'
    # Every sort button keeps the filter of the list it sorts.
    assert all('category=never_watched' in href for href in sort_hrefs(html).values())
    filter_nav = re.search(r'<nav class="category-filters" aria-label="[^"]*">(.*?)</nav>',
                           html, re.S)
    filter_link = filter_nav and re.search(r'class="btn filter-button active" href="([^"]+)"',
                                           filter_nav.group(1))
    assert filter_link and 'sort=age' in filter_link.group(1).replace('&amp;', '&')


def test_the_category_filter_still_selects_its_own_rows_by_default(app, conn, client):
    publish(conn)

    html = page(client, tab='candidates', category='stalled_series').get_data(as_text=True)

    assert titles(html) == ['Beta']
    assert active_sort(html) == 'size'


def test_the_pager_keeps_the_chosen_sort(app, conn, client):
    """Sixty rows: the second page continues the order the owner chose."""
    publish(conn)
    # The four rows above plus fifty-six synthetic ones, all with a size.
    rows = [
        display_row(1000 + index, f'Extra {index:02d}', 100 + index, 5.0, None)
        for index in range(56)
    ]
    report = json.loads(conn.execute('SELECT report FROM scan_runs').fetchone()[0])
    report['detection']['counts']['candidate_rows'].extend(rows)
    report['detection']['counts']['candidates'] += len(rows)
    conn.execute('UPDATE scan_runs SET report = ?', (json.dumps(report),))
    conn.commit()

    first = page(client, tab='candidates', sort='age').get_data(as_text=True)
    next_href = re.search(r'rel="next" href="([^"]+)"', first).group(1).replace('&amp;', '&')
    second = client.get(next_href).get_data(as_text=True)

    assert 'sort=age' in next_href
    # Added longest ago first; the rows without a date follow, by title.
    assert titles(first) == ['Alpha', 'Gamma', 'Beta', 'Delta'] + [
        f'Extra {i:02d}' for i in range(46)]
    assert titles(second) == [f'Extra {i:02d}' for i in range(46, 56)]


def test_a_row_action_returns_to_the_same_sorted_view(app, conn, client):
    publish(conn)
    html = page(client, tab='candidates', sort='rating').get_data(as_text=True)
    form = re.search(r'<form method="post" action="([^"]*/mark[^"]*)">(.*?)</form>', html, re.S)
    assert form, html[:400]
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"',
                             form.group(2)))
    assert fields['sort'] == 'rating'

    response = client.post(form.group(1).replace('&amp;', '&'), data=fields)

    assert response.status_code == 303
    location = response.headers['Location']
    assert 'sort=rating' in location and 'tab=candidates' in location
    landing = client.get(location.split('#')[0]).get_data(as_text=True)
    assert active_sort(landing) == 'rating'
    assert titles(landing) == list(EXPECTED['rating'])


# -- translated, named and large enough to press ------------------------------

@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_the_sort_controls_are_translated_in_every_language(app, conn, client, locale):
    publish_leaving(conn)

    html = page(client, tab='leaving_soon', lang=locale).get_data(as_text=True)
    # The rendered text is escaped; compare it with the catalog as it reads.
    nav = unescape(sort_nav(html))

    for key in LEAVING_SORT_KEYS:
        assert translate(f'sort.{key}', locale) in nav, (locale, key)


def test_the_three_catalogs_share_the_sort_keys():
    catalogs = load_catalogs()
    keys = {f'sort.{name}' for name in ('label',) + LEAVING_SORT_KEYS}
    for locale in ('en', 'ca', 'es'):
        assert keys <= set(catalogs[locale]), locale


def test_the_sort_bar_is_a_named_navigation_of_44px_buttons(app, conn, client):
    publish(conn)

    html = page(client, tab='candidates').get_data(as_text=True)

    # A second labelled navigation, not a list of links in running text: the
    # buttons inherit the 44 px target of .btn at every width.
    nav = re.search(r'<nav class="category-filters sort-filters" aria-label="([^"]+)">', html)
    assert nav and nav.group(1) == translate('sort.label', 'en')
    assert set(sort_hrefs(html)) == set(SORT_KEYS)
    assert all('class="btn filter-button' in link
               for link in re.findall(r'<a class="btn filter-button[^"]*"', sort_nav(html)))


def test_a_leaving_soon_row_action_returns_to_the_same_sorted_view(app, conn, client):
    publish_leaving(conn)
    html = page(client, tab='leaving_soon', sort='size').get_data(as_text=True)
    form = re.search(r'<form method="post" action="([^"]*/keep[^"]*)">(.*?)</form>', html, re.S)
    assert form, html[:400]
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"',
                             form.group(2)))
    assert fields['sort'] == 'size'

    response = client.post(form.group(1).replace('&amp;', '&'), data=fields)

    assert response.status_code == 303
    location = response.headers['Location']
    assert 'sort=size' in location and 'tab=leaving_soon' in location
    landing = client.get(location.split('#')[0]).get_data(as_text=True)
    assert active_sort(landing) == 'size'
