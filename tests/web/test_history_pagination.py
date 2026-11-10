"""The History page paginates and the CSV export streams (issue #9)."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def seed_events(conn, count, outcome='deleted', month='2026-11'):
    for index in range(count):
        conn.execute(
            "INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,"
            " disk, reason, marked_by, occurred_at) VALUES (?, ?, 'movie', 100, ?,"
            " 'disk3', 'A reason', 'owner', ?)",
            (outcome, f"Film {index}", index, (BASE + timedelta(minutes=index)).isoformat()),
        )
    conn.commit()


def test_history_page_shows_the_newest_events_and_the_next_page_holds_the_older_ones(conn):
    from almali_library_curator.web.views import history_view

    seed_events(conn, 120)

    first = history_view(conn)
    second = history_view(conn, page=2)
    third = history_view(conn, page=3)

    assert [len(first.events), len(second.events), len(third.events)] == [50, 50, 20]
    assert (first.has_next, first.has_previous) == (True, False)
    assert (second.has_next, second.has_previous) == (True, True)
    assert (third.has_next, third.has_previous) == (False, True)
    titles = [row.title for row in first.events + second.events + third.events]
    assert titles == [f"Film {index}" for index in reversed(range(120))]


def test_history_page_keeps_the_filters(conn):
    from almali_library_curator.web.views import history_view

    seed_events(conn, 3, outcome='deleted', month='2026-11')
    seed_events(conn, 2, outcome='failed', month='2026-10')

    model = history_view(conn, outcome='deleted', month='2026-11')

    assert [row.outcome for row in model.events] == ['deleted'] * 3
    assert (model.outcome, model.month) == ('deleted', '2026-11')
    assert model.freed_per_month == {'2026-11': 3}


def test_history_page_keeps_the_order_of_the_actual_instants(conn):
    from almali_library_curator.web.views import history_view

    conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,"
        " disk, reason, marked_by, occurred_at)"
        " VALUES ('deleted', 'Earlier', 'movie', 100, 0, 'disk3', 'A reason', 'owner',"
        " '2026-11-01T12:00:00+01:00')"
    )
    conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,"
        " disk, reason, marked_by, occurred_at)"
        " VALUES ('deleted', 'Later', 'movie', 100, 0, 'disk3', 'A reason', 'owner',"
        " '2026-11-01T11:30:00+00:00')"
    )
    conn.commit()

    assert [row.title for row in history_view(conn).events] == ['Later', 'Earlier']


def test_history_links_keep_the_filters(client, conn):
    seed_events(conn, 120)

    response = client.get('/history?outcome=deleted&month=2026-11')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'page=2' in body
    assert 'outcome=deleted' in body
    assert 'month=2026-11' in body


def test_history_page_beyond_the_end_answers_the_last_page(client, conn):
    seed_events(conn, 60)

    response = client.get('/history?page=1000000000000000000')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Film 0' in body


def test_history_csv_exports_every_row_and_not_only_the_page(client, conn):
    seed_events(conn, 120)

    response = client.get('/history.csv')

    assert response.status_code == 200 and response.mimetype == 'text/csv'
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 120
    assert rows[0]['title'] == 'Film 119'
