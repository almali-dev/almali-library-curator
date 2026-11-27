"""#141: a History card shows the item's poster, like every other card.

The card asks the app's own ``/poster/<id>`` route, exactly as the Library
cards do (#139); an event without an item, or whose item has no usable
thumb, gets the same request-free placeholder. The CSV export keeps its
published columns.
"""

from __future__ import annotations

import csv
import io
import re

THUMB = '/library/metadata/11/thumb/1700000000'


def seed_item(conn, thumb):
    library = conn.execute(
        "INSERT INTO libraries (plex_key, title, section_type) VALUES ('1', 'Movies', 'movie')"
    ).lastrowid
    return conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type, thumb)"
        " VALUES (?, '11', 'Tenet', 'movie', ?)", (library, thumb)).lastrowid


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


def test_a_history_card_shows_the_poster_of_its_item(client, conn):
    identifier = seed_item(conn, THUMB)
    seed_event(conn, identifier)

    html = client.get('/history?lang=en').get_data(as_text=True)

    [card] = history_cards(html)
    assert re.findall(r'<img class="poster" src="([^"]+)"', card) == [f'/poster/{identifier}']
    assert 'poster--none' not in card
    assert THUMB not in html
    assert 'Tenet' in card


def test_an_event_without_a_usable_poster_shows_the_placeholder(client, conn):
    identifier = seed_item(conn, 'https://evil.example/x.jpg')
    seed_event(conn, identifier, 'Invalid thumb')
    seed_event(conn, None, 'No item')

    html = client.get('/history?lang=en').get_data(as_text=True)

    cards = history_cards(html)
    assert len(cards) == 2
    for card in cards:
        assert 'poster--none' in card
    assert '/poster/' not in html
    assert 'evil.example' not in html


def test_the_csv_export_keeps_its_published_columns(client, conn):
    from almali_library_curator.web.views import EVENT_COLUMNS

    seed_event(conn, seed_item(conn, THUMB))

    body = client.get('/history.csv?lang=en').get_data(as_text=True)

    header = next(csv.reader(io.StringIO(body)))
    assert len(header) == len(EVENT_COLUMNS)
    assert 'thumb' not in body and THUMB not in body
