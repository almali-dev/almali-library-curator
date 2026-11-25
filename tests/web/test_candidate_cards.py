"""#128: the candidate card says what it deletes and hides an unknown disk.

The card reads its display row from the scan_rows cache and the facts of the
scope (its seasons and episodes) from the synced rows, so these tests drive
the real Library page over a real migrated database.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from markupsafe import Markup

from almali_library_curator.config import save_settings
from almali_library_curator.lifecycle import LifecycleEngine
from lifecycle_helpers import make_candidate, seed_episode, seed_item, seed_library

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000


def candidate_row(identifier, title, size, *, unit='series', seasons=(), categories=('never_watched',),
                  disk='disk3', rating=7.4, rating_source='Plex'):
    return dict(media_item_id=identifier, title=title, unit=unit, seasons=list(seasons),
                type_badge=unit.upper(), categories=list(categories), reasons=['A reason.'],
                reason='A reason.', size_bytes=size, disk=disk, rating=rating,
                rating_source=rating_source)


def publish(conn, candidates, protected=()):
    """Record a finished scan; the migration-014 triggers cache its rows."""
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


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def scope(html):
    """The card's scope sentence, normalized, or None when it has none."""
    match = re.search(r'<p class="candidate-scope text-muted">(.*?)</p>', html, re.S)
    if match is None:
        return None
    return ' '.join(Markup(match.group(1)).striptags().split())


def seeded_series(conn):
    """A series with two seasons and four episodes, and its media item id."""
    library = seed_library(conn, '2', 'Shows', section_type='show')
    series_id = seed_item(conn, library, '200', 'Foundation', item_type='series')
    for number in (1, 2):
        for episode_number in (1, 2):
            seed_episode(conn, '200', number, episode_number)
    return series_id


def test_a_series_candidate_says_the_whole_series_and_what_it_holds(app, conn, client):
    series_id = seeded_series(conn)
    publish(conn, [candidate_row(series_id, 'Foundation', 80 * GB)])

    html = client.get('/?lang=en').get_data(as_text=True)

    assert scope(html) == 'Whole series · 2 seasons · 4 episodes'


def test_a_seasons_candidate_names_the_seasons_and_how_many_episodes(app, conn, client):
    series_id = seeded_series(conn)
    publish(conn, [candidate_row(series_id, 'Foundation S2', 30 * GB, unit='seasons',
                                 seasons=(2,), categories=('stalled_series',))])

    html = client.get('/?lang=en').get_data(as_text=True)

    assert 'Foundation S2' in text(client.get('/?lang=en'))
    # The title already names the seasons, so the scope repeats no season count.
    assert scope(html) == '2 episodes'


def test_the_scope_is_translated(app, conn, client):
    series_id = seeded_series(conn)
    publish(conn, [candidate_row(series_id, 'Foundation', 80 * GB)])

    html = client.get('/?lang=ca').get_data(as_text=True)

    assert scope(html) == 'Sèrie completa · 2 temporades · 4 capítols'


def test_a_movie_card_stays_as_it_was(app, conn, client):
    library = seed_library(conn, '1', 'Movies')
    movie_id = seed_item(conn, library, '100', 'Tenet')
    publish(conn, [candidate_row(movie_id, 'Tenet', 24 * GB, unit='movie')])

    html = client.get('/?lang=en').get_data(as_text=True)

    assert 'Tenet' in text(client.get('/?lang=en'))
    assert scope(html) is None


def configure_host(app, unraid_root):
    save_settings(replace(app.config['SETTINGS'], unraid_root=unraid_root))


def test_the_card_hides_the_disk_when_the_host_is_not_configured(app, conn, client):
    configure_host(app, '')
    library = seed_library(conn, '1', 'Movies')
    movie_id = seed_item(conn, library, '100', 'Tenet')
    publish(conn, [candidate_row(movie_id, 'Tenet', 24 * GB, unit='movie')])

    body = text(client.get('/?lang=en'))

    assert 'disk3' not in body
    assert 'Disk' not in body


def test_the_card_shows_the_disk_when_the_host_is_configured(app, conn, client):
    configure_host(app, '/mnt/user')
    library = seed_library(conn, '1', 'Movies')
    movie_id = seed_item(conn, library, '100', 'Tenet')
    publish(conn, [candidate_row(movie_id, 'Tenet', 24 * GB, unit='movie')])

    body = text(client.get('/?lang=en'))

    assert 'Disk' in body and 'disk3' in body


def leaving_item(conn, settings, *, disk='disk3'):
    """A real removal in Leaving soon, on a seeded row that knows its disk."""
    library = seed_library(conn, '1', 'Movies')
    movie_id = seed_item(conn, library, '100', 'Tenet')
    if disk is not None:
        conn.execute("UPDATE media_items SET disk=? WHERE id=?", (disk, movie_id))
        conn.commit()
    candidate = make_candidate(movie_id, '100', 'Tenet')
    LifecycleEngine(conn, settings=settings, clock=lambda: datetime.now(timezone.utc)) \
        .mark_for_removal(candidate, marked_by='owner')
    return movie_id


def leaving_body(client):
    return text(client.get('/?tab=leaving_soon&lang=en'))


def test_the_leaving_card_hides_the_disk_when_the_host_is_not_configured(app, conn, client):
    configure_host(app, '')
    leaving_item(conn, app.config['SETTINGS'])

    body = leaving_body(client)

    assert 'Tenet' in body
    assert 'disk3' not in body
    assert 'Disk' not in body


def test_the_leaving_card_shows_the_disk_when_the_host_is_configured(app, conn, client):
    configure_host(app, '/mnt/user')
    leaving_item(conn, app.config['SETTINGS'])

    body = leaving_body(client)

    assert 'Tenet' in body
    assert 'Disk' in body and 'disk3' in body


def test_the_leaving_card_still_names_an_unknown_disk_when_configured(app, conn, client):
    configure_host(app, '/mnt/user')
    leaving_item(conn, app.config['SETTINGS'], disk=None)

    body = leaving_body(client)

    assert 'Disk' in body and 'Unknown' in body
