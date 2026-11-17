"""#63: a row action returns to the same tab, filter, page and row.

Every test drives the real screen: it reads the hidden fields the browser
would post from the rendered HTML, posts them to the real route and follows
the redirect the app answers. A row action used to land on Candidates/All at
the top of the page, which made triage of a long list slow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import json
import re

import pytest

from lifecycle_helpers import seed_item, seed_library


NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000

ROW_PATTERN = re.compile(
    r'<article class="[^"]*candidate-row[^"]*"(?P<attrs>[^>]*)>(?P<body>.*?)</article>', re.S)
ID_PATTERN = re.compile(r'\bid="([^"]+)"')
FORM_PATTERN = re.compile(r'<form method="post" action="([^"]+)">(.*?)</form>', re.S)
INPUT_PATTERN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')


# -- fixtures over the real cache and the real routes -------------------------

def scan_row(identifier, title, *, size_bytes, categories=('never_watched',)):
    """One display row of a published scan, as the runner writes it."""
    return dict(media_item_id=identifier, title=title, unit='movie', seasons=[],
                type_badge='MOVIE', categories=list(categories),
                reasons=['Added April 2024.'], reason='Added April 2024.',
                reason_parts=[{'key': 'detection.reason.never_watched.movie',
                               'params': {'date': '2024-04-01', 'users': 3}}],
                size_bytes=size_bytes, disk='disk1', rating=7.0, rating_source='IMDb')


def publish_scan(conn, candidates):
    """Record a finished scan; migration 014 caches its rows for the screen."""
    report = json.dumps({
        'sync': {'status': 'finished'}, 'enrichment': {'status': 'finished'},
        'detection': {'status': 'finished', 'counts': {
            'candidates': len(candidates), 'protected': 0,
            'candidate_rows': list(candidates), 'protected_rows': []}},
        'recheck': {'status': 'finished'}, 'deletion': {'status': 'finished'},
    })
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger,"
        " candidates_found, report) VALUES (?, ?, 'finished', 'dry_run', 'manual', ?, ?)",
        (NOW.isoformat(), NOW.isoformat(), len(candidates), report))
    conn.commit()


def seeded_library(conn):
    """Three real movies behind one published scan, largest first."""
    library = seed_library(conn, '1', 'Movies')
    tenet = seed_item(conn, library, '11', 'Tenet', rating=7.0)
    heat = seed_item(conn, library, '12', 'Heat', rating=7.0)
    up = seed_item(conn, library, '13', 'Up', rating=7.0)
    publish_scan(conn, [scan_row(tenet, 'Tenet', size_bytes=24 * GB),
                        scan_row(heat, 'Heat', size_bytes=8 * GB,
                                 categories=('stalled_series',)),
                        scan_row(up, 'Up', size_bytes=4 * GB)])
    return tenet, heat, up


def rows(html: str) -> list[tuple[str | None, list[tuple[str, dict[str, str]]]]]:
    """The rendered rows: the row's id and the forms it carries, as posted."""
    found = []
    for match in ROW_PATTERN.finditer(html):
        identifier = ID_PATTERN.search(match.group('attrs'))
        forms = [(unescape(action), dict((name, unescape(value)) for name, value in
                                         INPUT_PATTERN.findall(body)))
                 for action, body in FORM_PATTERN.findall(match.group('body'))]
        found.append((identifier.group(1) if identifier else None, forms))
    return found


def only_row(html: str) -> tuple[str, list[tuple[str, dict[str, str]]]]:
    """The single row of a one-row page, with its id."""
    rendered = rows(html)
    assert len(rendered) == 1, rendered
    anchor, forms = rendered[0]
    assert anchor and forms
    return anchor, forms


def form_of(forms, path: str) -> tuple[str, dict[str, str]]:
    """The form whose action path ends with ``path``."""
    return next(form for form in forms if form[0].split('?')[0].endswith(path))


def page(client, **params):
    query = '&'.join(f'{name}={value}' for name, value in params.items())
    return client.get('/?' + query)


def body_at(client, location: str):
    """The page a redirect points at, without the fragment a browser keeps."""
    return client.get(location.split('#')[0]).get_data(as_text=True)


# -- the rendered contract: every row posts its view and its own row ----------

def test_a_candidate_row_posts_the_tab_the_filter_and_the_row(app, client, conn):
    seeded_library(conn)

    html = page(client, tab='candidates', category='stalled_series').get_data(as_text=True)

    anchor, forms = only_row(html)
    assert anchor.startswith('item-')
    for _, fields in forms:
        assert fields['tab'] == 'candidates'
        assert fields['category'] == 'stalled_series'
        assert fields['anchor'] == anchor


def test_a_leaving_row_posts_the_tab_and_the_row_it_belongs_to(app, client, conn):
    tenet, _, _ = seeded_library(conn)
    assert client.post(f'/items/{tenet}/mark', data={'unit': 'movie'}).status_code == 303
    request_id = conn.execute('SELECT id FROM removal_requests').fetchone()[0]

    html = page(client, tab='leaving_soon').get_data(as_text=True)

    anchor, forms = only_row(html)
    action, fields = form_of(forms, '/keep')
    assert anchor == f'request-{request_id}'
    assert action.split('?')[0].endswith(f'/requests/{request_id}/keep')
    assert fields['tab'] == 'leaving_soon' and fields['anchor'] == anchor


# -- what the owner gets back -------------------------------------------------

def test_marking_returns_to_the_same_tab_filter_and_row(app, client, conn):
    """The reported bug: a mark landed on Candidates/All at the top."""
    seeded_library(conn)
    html = page(client, tab='candidates', category='stalled_series').get_data(as_text=True)
    anchor, forms = only_row(html)
    action, fields = form_of(forms, '/mark')

    response = client.post(action, data=fields)

    assert response.status_code == 303
    location = response.headers['Location']
    assert 'tab=candidates' in location and 'category=stalled_series' in location
    assert location.endswith('#' + anchor)
    # The redirect really lands on that view: same filter, same row, and the
    # row now carries the way back (Undo).
    body = body_at(client, location)
    assert f'id="{anchor}"' in body
    assert re.search(r'class="tab-link active"[^>]*href="[^"]*tab=candidates', body)
    assert re.search(r'class="btn filter-button active"[^>]*href="[^"]*category=stalled_series',
                     body)
    landing = next(forms for identifier, forms in rows(body) if identifier == anchor)
    assert any(link.split('?')[0].endswith('/undo') for link, _ in landing)


def test_keeping_an_item_returns_to_the_row_it_became(app, client, conn):
    tenet, _, _ = seeded_library(conn)
    assert client.post(f'/items/{tenet}/mark', data={'unit': 'movie'}).status_code == 303
    request_id = conn.execute('SELECT id FROM removal_requests').fetchone()[0]
    anchor, forms = only_row(page(client, tab='leaving_soon').get_data(as_text=True))
    action, fields = form_of(forms, '/keep')

    response = client.post(action, data=fields)

    assert response.status_code == 303
    location = response.headers['Location']
    assert 'tab=leaving_soon' in location and location.endswith('#' + anchor)
    # The cancelled removal keeps the same anchor, so the owner lands on the
    # row the action moved instead of the top of the page.
    body = body_at(client, location)
    assert f'id="{anchor}"' in body and 'cancelled-row' in body
    assert conn.execute('SELECT state FROM removal_requests WHERE id=?',
                        (request_id,)).fetchone()[0] == 'cancelled'


def test_a_row_on_a_later_page_returns_to_that_page(app, client, conn):
    seeded_library(conn)
    html = page(client, tab='candidates', page=2, per_page=2).get_data(as_text=True)
    anchor, forms = only_row(html)
    action, fields = form_of(forms, '/mark')

    response = client.post(action, data=fields)

    location = response.headers['Location']
    assert 'page=2' in location and 'per_page=2' in location
    assert location.endswith('#' + anchor)
    body = body_at(client, location)
    assert 'Page 2 of 2' in body and f'id="{anchor}"' in body


# -- the posted view is checked, never trusted --------------------------------

@pytest.mark.parametrize('posted,dropped', [
    ({'tab': 'nonsense'}, 'tab='),
    ({'tab': 'candidates', 'category': '../etc/passwd'}, 'category='),
    ({'tab': 'candidates', 'anchor': '<script>alert(1)</script>'}, '#'),
    ({'tab': 'candidates', 'page': '0'}, 'page='),
    ({'tab': 'candidates', 'per_page': '100000'}, 'per_page='),
])
def test_a_tampered_view_is_dropped_instead_of_reflected(app, client, conn, posted, dropped):
    tenet, _, _ = seeded_library(conn)

    response = client.post(f'/items/{tenet}/mark', data={'unit': 'movie', **posted})

    assert response.status_code == 303
    assert dropped not in response.headers['Location']
    # The action itself still happened; only the untrusted view was dropped.
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 1


# -- the neighbours of the change still hold ---------------------------------

def test_an_action_without_a_view_keeps_the_plain_redirect(app, client, conn):
    library = seed_library(conn, '1', 'Movies')
    identifier = seed_item(conn, library, '11', 'Tenet', rating=7.0)

    response = client.post(f'/items/{identifier}/mark', data={'unit': 'movie'})

    assert response.status_code == 303
    assert response.headers['Location'] == '/?lang=en'


def test_another_screen_ignores_a_posted_library_view(app, client, conn):
    response = client.post('/settings/kill-switch',
                           data={'enabled': 'on', 'tab': 'candidates', 'anchor': 'item-1'})

    assert response.status_code == 303
    assert response.headers['Location'] == '/settings?lang=en'
