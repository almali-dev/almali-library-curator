"""S1 / #48: the Library and Protected tabs are paged, filtered and sorted in SQL.

The scan publishes its display rows into the cache of migration 014, one row
per candidate and per protected item. These tests drive the real screen over
250 candidates: one page at a time, the same order the screen always had, and
a Leaving soon tab that never reads a candidate row at all.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import re

import pytest

from almali_library_curator.app import create_app
from almali_library_curator.config import save_settings
from almali_library_curator.db import MIGRATIONS, SCHEMA_VERSION, connect, init_db
from almali_library_curator.scan import rows as scan_rows

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000
CANDIDATES = 250


def candidate_row(index: int) -> dict:
    """One display row, largest first when sorted by ``size_bytes``."""
    categories = ['never_watched'] + (['stalled_series'] if index % 2 == 0 else [])
    return dict(
        media_item_id=index + 1, title=f'Item {index + 1:03d}', unit='movie',
        seasons=[], type_badge='MOVIE', categories=categories,
        reasons=['Added January 2024.'], reason='Added January 2024.',
        reason_parts=[{'key': 'detection.reason.never_watched.movie',
                       'params': {'date': '2024-01-15', 'users': 3}}],
        size_bytes=(CANDIDATES - index) * GB, disk='disk1', rating=7.0,
        rating_source='IMDb',
    )


def protected_row(index: int) -> dict:
    return dict(candidate_row(index), protection_kind='rating',
                protection_reason='Rated 7.0, at or above the minimum of 6.0.',
                protection_reason_parts=[{'key': 'protection.reason.rating',
                                          'params': {'rating': '7.0', 'minimum': '6.0'}}])


def publish(conn, candidates, protected=()):
    """Record a finished scan, as the runner does: the triggers cache its rows."""
    report = json.dumps({
        'sync': {'status': 'finished'}, 'enrichment': {'status': 'finished'},
        'detection': {'status': 'finished', 'counts': {
            'candidates': len(candidates), 'protected': len(protected),
            'candidate_rows': list(candidates), 'protected_rows': list(protected)}},
        'recheck': {'status': 'finished'}, 'deletion': {'status': 'finished'},
    })
    cursor = conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger,"
        " candidates_found, report) VALUES (?, ?, 'finished', 'dry_run', 'manual', ?, ?)",
        (NOW.isoformat(), NOW.isoformat(), len(candidates), report))
    conn.commit()
    return cursor.lastrowid


def titles(html: str) -> list[str]:
    return re.findall(r'<h3 class="candidate-title">([^<]+)</h3>', html)


def page(client, **params):
    query = '&'.join(f'{name}={value}' for name, value in params.items())
    return client.get('/?' + query)


def test_the_library_writes_one_page_of_rows_and_a_pager(app, conn, client):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)])

    response = page(client, tab='candidates')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert titles(html) == [f'Item {index + 1:03d}' for index in range(50)]
    assert '<nav class="pager" aria-label="Pages">' in html
    assert 'Page 1 of 5' in html and '250 items' in html
    assert 'rel="next"' in html and 'rel="prev"' not in html
    # 250 candidates used to be rendered whole; one page stays bounded.
    assert len(response.data) < 250_000


def test_page_three_continues_the_order_the_screen_always_had(app, conn, client):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)])

    html = page(client, tab='candidates', page=3).get_data(as_text=True)

    assert titles(html) == [f'Item {index + 1:03d}' for index in range(100, 150)]
    assert 'Page 3 of 5' in html
    assert 'rel="prev"' in html and 'rel="next"' in html


def test_the_sort_is_the_same_across_every_page(app, conn, client):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)])

    seen = []
    for number in range(1, 6):
        seen += titles(page(client, tab='candidates', page=number).get_data(as_text=True))

    # Largest first, then title, then item id: no gaps, no repeats.
    assert seen == [f'Item {index + 1:03d}' for index in range(CANDIDATES)]
    assert len(set(seen)) == CANDIDATES


def test_a_filter_is_applied_to_every_page(app, conn, client):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)])
    stalled = [f'Item {index + 1:03d}' for index in range(0, CANDIDATES, 2)]

    first = page(client, tab='candidates', category='stalled_series').get_data(as_text=True)
    last = page(client, tab='candidates', category='stalled_series',
                page=3).get_data(as_text=True)

    assert titles(first) == stalled[:50]
    assert titles(last) == stalled[100:]
    assert 'Page 1 of 3' in first and 'Page 3 of 3' in last
    assert '125 items' in first
    # The pager keeps the filter of the page it is on.
    next_href = re.search(r'rel="next" href="([^"]+)"', first).group(1)
    assert 'tab=candidates' in next_href and 'category=stalled_series' in next_href
    assert 'page=2' in next_href


def test_the_page_size_is_bounded_and_the_language_survives_the_pager(app, conn, client):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)])

    small = page(client, tab='candidates', per_page=10, lang='ca').get_data(as_text=True)
    huge = page(client, tab='candidates', per_page=1000).get_data(as_text=True)

    assert len(titles(small)) == 10
    # 100 is the largest page a request can ask for.
    assert len(titles(huge)) == 100
    next_href = re.search(r'rel="next" href="([^"]+)"', small).group(1)
    assert 'lang=ca' in next_href and 'per_page=10' in next_href


def test_the_protected_tab_is_paged_but_the_leaving_soon_tab_is_not(
        app, conn, client, monkeypatch
):
    publish(conn, [candidate_row(index) for index in range(CANDIDATES)],
            [protected_row(index) for index in range(3)])

    calls = []
    original = scan_rows.page_rows

    def counting(conn, kind, **kwargs):
        calls.append(kind)
        return original(conn, kind, **kwargs)

    monkeypatch.setattr(scan_rows, 'page_rows', counting)

    assert client.get('/?tab=leaving_soon').status_code == 200
    assert calls == []

    protected = page(client, tab='protected').get_data(as_text=True)
    assert calls == ['protected']
    assert titles(protected) == ['Item 001', 'Item 002', 'Item 003']

    candidates = page(client, tab='candidates').get_data(as_text=True)
    assert calls == ['protected', 'candidate']
    assert len(titles(candidates)) == 50


def test_an_upgraded_v13_database_shows_its_latest_scan_immediately(
        settings, owner_client_class
):
    """Migration 014 backfills the cache, so the upgrade is not an empty page."""
    save_settings(settings)
    with closing(connect(settings.database_path)) as conn:
        for script in MIGRATIONS[:13]:
            conn.executescript(script)
        conn.execute('PRAGMA user_version = 13')
        publish(conn, [candidate_row(0), candidate_row(1)], [protected_row(2)])
        assert conn.execute('SELECT COUNT(*) FROM scan_run_views').fetchone()[0] == 1

    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True, SECRET_KEY='web-test-session', WTF_CSRF_ENABLED=False)
    app.test_client_class = owner_client_class
    client = app.test_client()

    with closing(connect(settings.database_path)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('SELECT COUNT(*) FROM scan_rows').fetchone()[0] == 3
    html = client.get('/').get_data(as_text=True)
    assert titles(html) == ['Item 001', 'Item 002']

