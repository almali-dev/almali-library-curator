"""S2 (#153): every item card links to the item's IMDb page.

A stored ``imdb_id`` becomes ``https://www.imdb.com/title/<id>/``; without
one the link is IMDb's own search for the item's title and year, URL-encoded,
built by the app's single helper (``almali_library_curator.imdb``). A stored
value that is not an IMDb id is never the address, and a row with neither id
nor title shows no link at all. The dry run's rows travel through
``scan_rows``, exactly as their poster does, and the CSV export keeps its
published columns.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from html import unescape
import io
import csv
import json
import re

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from lifecycle_helpers import make_candidate, seed_item, seed_library

from almali_library_curator.config import save_settings
from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner
from almali_library_curator.scan.snapshot import candidate_snapshot

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000

IMDB_ID = 'tt1375666'
TITLE_PAGE = f'https://www.imdb.com/title/{IMDB_ID}/'
TENET_SEARCH = 'https://www.imdb.com/find/?q=Tenet+2020&s=tt'
UNSAFE_ID = 'https://evil.example/x'

#: The one link a card shows: its address, its accessible name, its tooltip
#: and its body (the IMDb logo plus the "opens elsewhere" mark).
LINK = re.compile(
    r'<a class="score-link score-link--imdb" href="([^"]+)"\s+target="_blank"\s+rel="noopener noreferrer"'
    r'\s+aria-label="([^"]*)"\s+title="([^"]*)">(.*?)</a>', re.S)


def imdb_links(html):
    """Every IMDb link of a page: its address (unescaped as a browser reads it),
    its accessible name, and "IMDb" when it shows the IMDb logo as its mark."""
    return [(unescape(href), label,
             'IMDb' if re.search(r'<img class="imdb-logo"[^>]*src="[^"]*imdb-logo\.svg', body) else body)
            for href, label, tooltip, body in LINK.findall(html)]


def page(client, url):
    return client.get(url).get_data(as_text=True)


# -- the fixtures ------------------------------------------------------------

def candidate_row(identifier, title, size, *, unit='movie', seasons=(), thumb=None,
                  imdb_url=None, categories=('never_watched',)):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=list(seasons),
                type_badge=unit.upper(), categories=list(categories),
                reasons=['A reason.'], reason='A reason.', size_bytes=size,
                disk='disk3', rating=7.4, rating_source='Plex', thumb=thumb,
                imdb_url=imdb_url)


def publish(conn, candidates, protected=()):
    """Record a finished dry run; the triggers cache its rows for the page."""
    report = json.dumps({
        'sync': {'status': 'finished'}, 'enrichment': {'status': 'finished'},
        'detection': {'status': 'finished', 'counts': {
            'candidates': len(candidates), 'protected': len(protected),
            'candidate_rows': list(candidates), 'protected_rows': list(protected)}},
        'recheck': {'status': 'finished'}, 'deletion': {'status': 'finished'},
    })
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger,"
        " candidates_found, report) VALUES (?, ?, 'finished', 'dry_run', 'manual', ?, ?)",
        (NOW.isoformat(), NOW.isoformat(), len(candidates), report))
    conn.commit()


def store(conn, media_item_id, **facts):
    """The facts the sync stored for one item: its year, its IMDb id, its title."""
    for column, value in facts.items():
        conn.execute(f"UPDATE media_items SET {column} = ? WHERE id = ?", (value, media_item_id))
    conn.commit()


# -- Candidates and Protected ------------------------------------------------

def test_a_candidate_with_a_stored_id_links_to_its_imdb_title_page(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, imdb_url=TITLE_PAGE)])

    html = page(client, '/?tab=candidates&lang=en')

    assert imdb_links(html) == [(TITLE_PAGE, 'Open Tenet on IMDb', 'IMDb')]


def test_a_candidate_without_an_id_links_to_a_search_of_its_title_and_year(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, imdb_url=TENET_SEARCH)])

    html = page(client, '/?tab=candidates&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TENET_SEARCH]


def test_the_link_opens_a_new_tab_without_the_opener(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, imdb_url=TITLE_PAGE)])

    html = page(client, '/?tab=candidates&lang=en')

    [tag] = re.findall(r'<a class="score-link score-link--imdb"[^>]*>', html)
    assert 'target="_blank"' in tag
    assert 'rel="noopener noreferrer"' in tag


def test_a_protected_card_shows_the_link(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '12', 'Severance')
    publish(conn, [], [dict(candidate_row(item, 'Severance', 10 * GB, imdb_url=TITLE_PAGE),
                            protection_kind='rating',
                            protection_reason='High rating')])

    html = page(client, '/?tab=protected&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TITLE_PAGE]


def test_a_card_with_no_link_shows_none(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert imdb_links(html) == []
    assert 'imdb.com' not in html
    assert 'score-link--imdb' not in html


# -- Leaving soon: open and cancelled ----------------------------------------

def marked(conn, app, *, title='Tenet', unit=Unit.MOVIE, seasons=(), series=False):
    library = seed_library(conn, '1', 'Shows' if series else 'Movies',
                           section_type='show' if series else 'movie')
    item = seed_item(conn, library, '11', title, item_type='series' if series else 'movie')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: NOW)
    return engine, item, engine.mark_for_removal(
        make_candidate(item, '11', title, unit=unit, seasons=seasons))


def test_an_open_leaving_soon_card_searches_for_the_item(app, conn, client):
    _, item, _ = marked(conn, app)
    store(conn, item, year=2020)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert imdb_links(html) == [(TENET_SEARCH, 'Open Tenet on IMDb', 'IMDb')]


def test_a_cancelled_leaving_soon_card_shows_the_link(app, conn, client):
    engine, item, request = marked(conn, app)
    store(conn, item, imdb_id=IMDB_ID, year=2020)
    engine.keep_it(request)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TITLE_PAGE]


def test_a_leaving_soon_item_with_a_stored_id_links_to_its_title_page(app, conn, client):
    _, item, _ = marked(conn, app)
    store(conn, item, imdb_id=IMDB_ID)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TITLE_PAGE]


def test_a_seasons_request_links_to_its_series_and_keeps_its_title(app, conn, client):
    """A seasons card says "1923 S1"; the search is for the series, never that."""
    _, series, _ = marked(conn, app, title='1923', unit=Unit.SEASONS, seasons=(1,),
                          series=True)
    store(conn, series, year=2022)

    html = page(client, '/?tab=leaving_soon&lang=en')

    [card] = re.findall(r'<article class="candidate-row card leaving-row".*?</article>',
                        html, re.S)
    assert '1923 S1' in card
    assert [href for href, _, _ in imdb_links(card)] == [
        'https://www.imdb.com/find/?q=1923+2022&s=tt']


def test_an_unknown_year_searches_the_title_alone(app, conn, client):
    marked(conn, app)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [
        'https://www.imdb.com/find/?q=Tenet&s=tt']


def test_a_stored_value_that_is_not_an_id_is_never_the_address(app, conn, client):
    _, item, _ = marked(conn, app)
    store(conn, item, imdb_id=UNSAFE_ID, year=2020)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TENET_SEARCH]
    assert 'evil.example' not in html


def test_the_search_encodes_a_title_with_an_ampersand_a_hash_and_accents(app, conn, client):
    _, item, _ = marked(conn, app, title='Amélie & #1')
    store(conn, item, year=2001)

    html = page(client, '/?tab=leaving_soon&lang=en')

    [href] = [href for href, _, _ in imdb_links(html)]
    assert href == 'https://www.imdb.com/find/?q=Am%C3%A9lie+%26+%231+2001&s=tt'
    assert '#1' not in href and 'é' not in href
    # The address stays one attribute: the encoded query, then the sort flag.
    assert href.split('?', 1)[1] == 'q=Am%C3%A9lie+%26+%231+2001&s=tt'


def test_a_leaving_soon_item_with_no_title_or_id_shows_no_link(app, conn, client):
    _, item, _ = marked(conn, app, title='Tenet')
    store(conn, item, title='', year=2020)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert imdb_links(html) == []
    assert 'imdb.com' not in html


# -- Duplicates ---------------------------------------------------------------

def duplicate_movie(conn, library_id, rating_key, title, **facts):
    item = seed_item(conn, library_id, rating_key, title)
    conn.execute("UPDATE media_items SET tmdb_id = 999, file_size_bytes = ? WHERE id = ?",
                 (10 * GB, item))
    conn.commit()
    store(conn, item, **facts)
    return item


def test_a_duplicates_group_shows_one_link_for_the_group(app, conn, client):
    movies = seed_library(conn, '1', 'Movies')
    other = seed_library(conn, '2', 'Other movies')
    duplicate_movie(conn, movies, '11', 'Tenet', imdb_id=IMDB_ID, year=2020)
    duplicate_movie(conn, other, '21', 'Tenet')

    html = page(client, '/duplicates?lang=en')

    assert imdb_links(html) == [(TITLE_PAGE, 'Open Tenet on IMDb', 'IMDb')]


def test_a_duplicates_group_without_an_id_searches_its_title(app, conn, client):
    movies = seed_library(conn, '1', 'Movies')
    other = seed_library(conn, '2', 'Other movies')
    duplicate_movie(conn, movies, '11', 'Tenet', year=2020)
    duplicate_movie(conn, other, '21', 'Tenet')

    html = page(client, '/duplicates?lang=en')

    assert [href for href, _, _ in imdb_links(html)] == [TENET_SEARCH]


def test_a_duplicates_group_without_a_title_or_id_shows_no_link(app, conn, client):
    movies = seed_library(conn, '1', 'Movies')
    other = seed_library(conn, '2', 'Other movies')
    duplicate_movie(conn, movies, '11', '', year=2020)
    duplicate_movie(conn, other, '21', '')

    html = page(client, '/duplicates?lang=en')

    assert imdb_links(html) == []
    assert 'imdb.com' not in html


# -- History ------------------------------------------------------------------

def seed_event(conn, media_item_id, title='Tenet'):
    conn.execute(
        "INSERT INTO removal_events (media_item_id, outcome, title, unit, size_bytes,"
        " freed_bytes, disk, reason, marked_by, occurred_at) VALUES"
        " (?, 'deleted', ?, 'movie', 100, 100, 'disk3', 'A reason', 'owner',"
        " '2026-11-20T10:00:00+00:00')", (media_item_id, title))
    conn.commit()


def history_cards(html):
    return re.findall(r'<article class="candidate-row card history-row">(.*?)</article>',
                      html, re.S)


def test_a_history_card_links_with_its_item(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    store(conn, item, imdb_id=IMDB_ID)
    seed_event(conn, item)

    html = page(client, '/history?lang=en')

    [card] = history_cards(html)
    assert imdb_links(card) == [(TITLE_PAGE, 'Open Tenet on IMDb', 'IMDb')]


def test_a_history_event_without_an_item_links_from_its_stored_title(app, conn, client):
    seed_event(conn, None, 'Tenet')

    html = page(client, '/history?lang=en')

    [card] = history_cards(html)
    assert [href for href, _, _ in imdb_links(card)] == [
        'https://www.imdb.com/find/?q=Tenet&s=tt']


def test_a_history_event_without_an_item_or_a_title_shows_no_link(app, conn, client):
    seed_event(conn, None, '')

    html = page(client, '/history?lang=en')

    [card] = history_cards(html)
    assert imdb_links(card) == []
    assert 'imdb.com' not in card


def test_the_csv_export_keeps_its_published_columns(app, conn, client):
    from almali_library_curator.web.views import EVENT_COLUMNS

    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    store(conn, item, imdb_id=IMDB_ID)
    seed_event(conn, item)

    body = page(client, '/history.csv?lang=en')

    header = next(csv.reader(io.StringIO(body)))
    assert len(header) == len(EVENT_COLUMNS)
    assert 'imdb_url' not in header
    assert 'imdb.com' not in body


# -- the translated text and its accessible name ------------------------------

@pytest.mark.parametrize("lang,label", [
    ('en', 'Open Tenet on IMDb'),
    ('ca', 'Obre Tenet a IMDb'),
    ('es', 'Abrir Tenet en IMDb'),
])
def test_the_link_is_translated_in_every_language(app, conn, client, lang, label):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, imdb_url=TITLE_PAGE)])

    html = page(client, f'/?tab=candidates&lang={lang}')

    assert imdb_links(html) == [(TITLE_PAGE, label, 'IMDb')]


# -- the snapshot the dry run publishes --------------------------------------

def test_a_candidate_snapshot_carries_the_link_of_its_item(conn):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    store(conn, item, imdb_id=IMDB_ID, year=2020)

    snapshot = candidate_snapshot(conn, make_candidate(item, '11', 'Tenet'))

    assert snapshot['imdb_url'] == TITLE_PAGE


def test_a_seasons_candidate_carries_the_series_link(conn):
    """The card says "1923 S2"; the snapshot searches for the series (S2)."""
    library = seed_library(conn, '2', 'Shows', section_type='show')
    series = seed_item(conn, library, '200', '1923', item_type='series')
    store(conn, series, year=2022)

    snapshot = candidate_snapshot(
        conn, make_candidate(series, '200', '1923 S2', unit=Unit.SEASONS, seasons=(2,)))

    assert snapshot['imdb_url'] == 'https://www.imdb.com/find/?q=1923+2022&s=tt'


def test_a_candidate_whose_item_row_is_gone_carries_no_link(conn):
    snapshot = candidate_snapshot(conn, make_candidate(999, '999', 'Gone'))

    assert snapshot['imdb_url'] is None


# -- a real dry run reaches the card through scan_rows -----------------------

def test_a_dry_run_candidate_carries_the_link_through_scan_rows(app, conn, client, monkeypatch):
    """S2: a dry run's items live only in the preview, so the link travels in
    the snapshot and the Library reads it from scan_rows, like the poster."""
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'test-token')
    data = FakePlexDataset()
    data.items['1'] = [movie('11', 'Tenet', rating=7.0, year=2020,
                             Guid=[{'id': f'imdb://{IMDB_ID}'}])]
    with FakePlexServer(data) as server:
        scanned = replace(app.config['SETTINGS'], plex_base_url=server.base_url,
                          plex_rate_limit_per_second=1000, dry_run=True,
                          scheduler_enabled=False)
        save_settings(scanned)
        report = ScanRunner(scanned, clock=lambda: NOW,
                            enrichment_step=lambda *args, **kwargs: {}).run('manual')

    assert report.status == 'finished'
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 0
    row = conn.execute("SELECT media_item_id, imdb_url FROM scan_rows"
                       " WHERE kind = 'candidate'").fetchone()
    assert row['imdb_url'] == TITLE_PAGE

    html = page(client, '/?tab=candidates&lang=en')

    assert imdb_links(html) == [(TITLE_PAGE, 'Open Tenet on IMDb', 'IMDb')]
    assert 'Tenet' in html


@pytest.mark.parametrize('stored', [
    'javascript:alert(1)',
    'https://evil.example/title/tt1375666/',
    'https://www.imdb.com.evil.example/title/tt1375666/',
    'https://www.imdb.com/title/tt1375666/?ref=x',
    'https://www.imdb.com/find/?q=Tenet&s=tt&x="><script>',
    'http://www.imdb.com/title/tt1375666/',
])
def test_a_stored_address_the_app_did_not_build_is_never_a_link(app, conn, client, stored):
    """S2: every href passes the helper's rule, also the snapshot's stored one."""
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, imdb_url=stored)])

    html = page(client, '/?tab=candidates&lang=en')

    assert imdb_links(html) == []
    assert 'score-link--imdb' not in html
    assert 'Tenet' in html



# -- #153 follow-up: the IMDb logo, visibly a link ---------------------------

def test_the_logo_is_a_served_svg_in_the_imdb_colours():
    from pathlib import Path

    svg = (Path(__file__).resolve().parents[2]
           / 'almali_library_curator/static/imdb-logo.svg').read_text(encoding='utf-8')
    assert svg.lstrip().startswith('<svg') and '#F5C518' in svg.upper().replace('#F5C518', '#F5C518')
    assert '<script' not in svg.lower()
