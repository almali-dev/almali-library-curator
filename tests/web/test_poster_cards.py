"""S1/S6 (#139): the item's poster on every card that shows an item.

The cards read the stored thumb: a candidate, a protected item and a dry
run's rows from the published ``scan_rows`` cache, a Leaving soon request
and a Duplicates group from the live ``media_items`` row. Only a stored
Plex thumb path becomes a poster; anything else shows a placeholder of the
same size and makes no request at all. The image is served by the app, so
neither the Plex token nor the Plex base URL ever reaches the page.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import re

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from lifecycle_helpers import make_candidate, seed_item, seed_library

from almali_library_curator.config import save_settings
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000

MOVIE_THUMB = "/library/metadata/11/thumb/1700000000"
SERIES_THUMB = "/library/metadata/200/thumb/1700000001"
UNSAFE_THUMB = "https://evil.example/x.jpg"

#: The app's own poster address, and the placeholder that makes no request.
POSTER_SRC = re.compile(r'<img class="poster" src="([^"]+)"')


def candidate_row(identifier, title, size, *, unit='movie', seasons=(), thumb=None,
                  categories=('never_watched',)):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=list(seasons),
                type_badge=unit.upper(), categories=list(categories),
                reasons=['A reason.'], reason='A reason.', size_bytes=size,
                disk='disk3', rating=7.4, rating_source='Plex', thumb=thumb)


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


def set_thumb(conn, media_item_id, thumb):
    conn.execute("UPDATE media_items SET thumb = ? WHERE id = ?", (thumb, media_item_id))
    conn.commit()


def page(client, url):
    return client.get(url).get_data(as_text=True)


def posters(html):
    return POSTER_SRC.findall(html)


def img_tag(html):
    return re.search(r'<img class="poster"[^>]*>', html).group(0)


# -- the Candidates and Protected cards --------------------------------------

def test_a_candidate_with_a_stored_thumb_renders_the_app_poster(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    set_thumb(conn, item, MOVIE_THUMB)
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, thumb=MOVIE_THUMB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert posters(html) == [f'/poster/{item}']
    tag = img_tag(html)
    assert 'alt=""' in tag and 'loading="lazy"' in tag
    assert re.search(r'width="\d+"', tag) and re.search(r'height="\d+"', tag)


def test_the_card_carries_neither_the_thumb_path_nor_the_token(app, conn, client):
    save_settings(replace(app.config['SETTINGS'],
                          plex_base_url='http://plex.example.invalid:32400'))
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    set_thumb(conn, item, MOVIE_THUMB)
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, thumb=MOVIE_THUMB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert f'/poster/{item}' in html
    assert MOVIE_THUMB not in html
    assert '/library/metadata' not in html
    assert 'plex.example.invalid' not in html
    assert 'X-Plex-Token' not in html


def test_a_seasons_row_shows_the_series_poster(app, conn, client):
    library = seed_library(conn, '2', 'Shows', section_type='show')
    series = seed_item(conn, library, '200', 'Lost in Space', item_type='series')
    set_thumb(conn, series, SERIES_THUMB)
    publish(conn, [candidate_row(series, 'Lost in Space', 80 * GB, unit='seasons',
                                 seasons=(1,), thumb=SERIES_THUMB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert posters(html) == [f'/poster/{series}']


def test_a_candidate_without_a_thumb_renders_the_placeholder(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert 'poster--none' in html
    assert posters(html) == []
    assert '/poster/' not in html


def test_an_invalid_thumb_renders_the_placeholder_and_makes_no_request(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    set_thumb(conn, item, UNSAFE_THUMB)
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, thumb=UNSAFE_THUMB)])

    html = page(client, '/?tab=candidates&lang=en')

    assert 'poster--none' in html
    assert posters(html) == []
    assert '/poster/' not in html
    assert 'evil.example' not in html


def test_a_protected_card_shows_its_poster(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '12', 'Severance')
    set_thumb(conn, item, MOVIE_THUMB)
    publish(conn, [], [dict(candidate_row(item, 'Severance', 10 * GB, thumb=MOVIE_THUMB),
                            protection_kind='rating',
                            protection_reason='High rating')])

    html = page(client, '/?tab=protected&lang=en')

    assert posters(html) == [f'/poster/{item}']


# -- Leaving soon: open and cancelled ----------------------------------------

def marked(conn, app):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    set_thumb(conn, item, MOVIE_THUMB)
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: NOW)
    return engine, item, engine.mark_for_removal(make_candidate(item, '11', 'Tenet'))


def test_an_open_leaving_soon_card_shows_the_poster(app, conn, client):
    marked(conn, app)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert posters(html) == [f'/poster/{conn.execute("SELECT id FROM media_items").fetchone()[0]}']


def test_a_cancelled_leaving_soon_card_shows_the_poster(app, conn, client):
    engine, item, request = marked(conn, app)
    engine.keep_it(request)

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert posters(html) == [f'/poster/{item}']


def test_a_leaving_soon_item_without_a_thumb_shows_the_placeholder(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: NOW)
    engine.mark_for_removal(make_candidate(item, '11', 'Tenet'))

    html = page(client, '/?tab=leaving_soon&lang=en')

    assert 'poster--none' in html
    assert posters(html) == []
    assert '/poster/' not in html


# -- Duplicates ---------------------------------------------------------------

def duplicate_movie(conn, library_id, rating_key, title, *, thumb=None):
    item = seed_item(conn, library_id, rating_key, title)
    conn.execute("UPDATE media_items SET tmdb_id = 999, file_size_bytes = ? WHERE id = ?",
                 (10 * GB, item))
    if thumb is not None:
        conn.execute("UPDATE media_items SET thumb = ? WHERE id = ?", (thumb, item))
    conn.commit()
    return item


def test_a_duplicates_group_shows_the_poster_beside_its_title(app, conn, client):
    movies = seed_library(conn, '1', 'Movies')
    other = seed_library(conn, '2', 'Other movies')
    first = duplicate_movie(conn, movies, '11', 'Tenet', thumb=MOVIE_THUMB)
    duplicate_movie(conn, other, '21', 'Tenet')

    html = page(client, '/duplicates?lang=en')

    assert posters(html) == [f'/poster/{first}']


def test_a_duplicates_group_without_a_thumb_shows_the_placeholder(app, conn, client):
    movies = seed_library(conn, '1', 'Movies')
    other = seed_library(conn, '2', 'Other movies')
    duplicate_movie(conn, movies, '11', 'Tenet')
    duplicate_movie(conn, other, '21', 'Tenet')

    html = page(client, '/duplicates?lang=en')

    assert 'poster--none' in html
    assert posters(html) == []
    assert '/poster/' not in html


# -- a real dry run reaches the card through scan_rows -----------------------

def test_a_dry_run_candidate_shows_the_thumb_through_scan_rows(app, conn, client, monkeypatch):
    """S6: a dry run's media_items live only in the preview, so the poster
    travels in the snapshot and the Library reads it from scan_rows."""
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'test-token')
    data = FakePlexDataset()
    data.items['1'] = [movie('11', 'Tenet', rating=7.0, thumb=MOVIE_THUMB)]
    with FakePlexServer(data) as server:
        scanned = replace(app.config['SETTINGS'], plex_base_url=server.base_url,
                          plex_rate_limit_per_second=1000, dry_run=True,
                          scheduler_enabled=False)
        save_settings(scanned)
        report = ScanRunner(scanned, clock=lambda: NOW,
                            enrichment_step=lambda *args, **kwargs: {}).run('manual')

    assert report.status == 'finished'
    # Nothing of the dry run's own database reached the live items.
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 0
    row = conn.execute("SELECT media_item_id, thumb FROM scan_rows WHERE kind = 'candidate'"
                       ).fetchone()
    assert row['thumb'] == MOVIE_THUMB

    html = page(client, '/?tab=candidates&lang=en')

    assert posters(html) == [f"/poster/{row['media_item_id']}"]
    assert 'Tenet' in html


# -- the rest of a movie card is unchanged -----------------------------------

def test_a_movie_card_keeps_its_title_reason_size_rating_and_actions(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet', rating=7.0)
    set_thumb(conn, item, MOVIE_THUMB)
    publish(conn, [candidate_row(item, 'Tenet', 10 * GB, thumb=MOVIE_THUMB)])

    html = page(client, '/?tab=candidates&lang=en')
    row = re.search(r'<article class="candidate-row card".*?</article>', html, re.S).group(0)

    assert 'Tenet' in row and 'A reason.' in row
    assert '10 GB' in row and '7.4' in row
    assert 'action=keep_forever' in row or '/keep-forever' in row
    assert 'Mark for removal' in row and 'Keep forever' in row
    assert f'/poster/{item}' in row
