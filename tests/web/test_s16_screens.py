"""S16 through the public screens.

Rule 1: a damaged scope carrier never breaks a page; the owner sees the
request as unreadable and the healthy rows render normally.
Rule 3: a stored failure follows the reader's language, and a sentence
stored by an older install still shows as written.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from markupsafe import escape

from almali_library_curator.detection.models import Unit
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from almali_library_curator.radarr import RadarrClient
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from lifecycle_helpers import (
    FakeCollections, FrozenClock, make_candidate, seed_episode, seed_item,
    seed_library,
)

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def mark_two(app, conn):
    library = seed_library(conn, '1', 'Movies')
    first = seed_item(conn, library, '11', 'Tenet')
    second = seed_item(conn, library, '12', 'Arrival')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'],
                             clock=lambda: NOW, collections=FakeCollections())
    engine.mark_for_removal(make_candidate(first, '11', 'Tenet'))
    engine.mark_for_removal(make_candidate(second, '12', 'Arrival'))
    conn.commit()
    return first, second


# -- rule 1 --------------------------------------------------------------------


def test_a_damaged_carrier_is_unreadable_on_every_screen_and_breaks_no_page(
        app, client, conn):
    first, second = mark_two(app, conn)
    conn.execute("UPDATE candidates SET facts='broken-json' WHERE media_item_id=?",
                 (first,))
    conn.commit()

    for path in ('/', '/?tab=leaving_soon', '/?tab=protected', '/history'):
        response = client.get(path)
        assert response.status_code == 200, path

    page = client.get('/?tab=leaving_soon').get_data(as_text=True)
    # The damaged request is named and shown as unreadable, not hidden.
    assert 'Tenet' in page
    assert translate('state.unreadable', 'en') in page
    assert translate('web.form.unreadable', 'en') in page
    # The healthy request renders normally and keeps its action.
    assert 'Arrival' in page
    assert translate('action.keep_it', 'en') in page


def test_a_damaged_carrier_does_not_break_the_candidates_tab(app, client, conn):
    first, second = mark_two(app, conn)
    conn.execute("UPDATE candidates SET facts='{not json' WHERE media_item_id=?",
                 (first,))
    conn.commit()

    response = client.get('/')

    assert response.status_code == 200


# -- rule 3 --------------------------------------------------------------------


def record_service_failure(app, conn, tmp_path):
    """Mark a movie, pass the final check and fail the deletion for real."""
    settings = replace(app.config['SETTINGS'], dry_run=False)
    library = seed_library(conn, '2', 'Movies')
    item = seed_item(conn, library, '21', 'Tenet')
    path = tmp_path / 'tenet.mkv'
    path.write_bytes(b'x' * 17)
    conn.execute("UPDATE media_items SET file_path=?, radarr_id=1 WHERE id=?",
                 (str(path), item))
    conn.commit()
    clock = FrozenClock()
    engine = LifecycleEngine(conn, settings=settings, clock=clock,
                             collections=FakeCollections())
    request = engine.mark_for_removal(make_candidate(item, '21', 'Tenet'))
    clock.advance(days=14)
    passed = [outcome.request.id for outcome in engine.run_recheck().passed]
    radarr = FakeRadarrDataset()
    radarr.movies.append(movie_resource(1, 'Tenet'))
    with FakeRadarrServer(radarr) as server:
        address = server.base_url
        server.stop()
        executor = DeletionExecutor(
            engine, radarr=RadarrClient(address, radarr.api_key,
                                        rate_limit_per_second=0, max_retries=0))
        outcome = executor.run_deletions(passed_ids=passed).outcomes[0]
    assert outcome.status.value == 'failed'
    return request.id


def test_failure_wording_follows_the_readers_language_while_the_stored_value_is_a_key(
        app, client, conn, tmp_path):
    request_id = record_service_failure(app, conn, tmp_path)
    stored = conn.execute('SELECT last_error FROM removal_requests WHERE id=?',
                          (request_id,)).fetchone()['last_error']
    assert stored == 'removal.failed.service'

    for locale in ('en', 'ca', 'es'):
        sentence = str(escape(translate('removal.failed.service', locale)))
        page = client.get(f'/?tab=leaving_soon&lang={locale}').get_data(as_text=True)
        assert sentence in page
        history = client.get(f'/history?lang={locale}').get_data(as_text=True)
        assert sentence in history

    # A locale change never rewrites history: the stored value stays the key.
    assert conn.execute('SELECT last_error FROM removal_requests WHERE id=?',
                        (request_id,)).fetchone()['last_error'] == 'removal.failed.service'


def test_a_sentence_stored_by_an_older_install_still_shows_as_written(app, client, conn):
    library = seed_library(conn, '3', 'Movies')
    item = seed_item(conn, library, '31', 'Tenet')
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'],
                             clock=lambda: NOW, collections=FakeCollections())
    request = engine.mark_for_removal(make_candidate(item, '31', 'Tenet'))
    legacy = "La eliminación ha fallado: RadarrUnavailableError."
    conn.execute("UPDATE removal_requests SET state='removal_failed', last_error=?"
                 " WHERE id=?", (legacy, request.id))
    conn.commit()

    page = client.get('/?tab=leaving_soon&lang=es').get_data(as_text=True)

    assert translate('state.removal_failed', 'es') in page
    assert legacy in page
    assert 'removal.failed' not in page


def test_a_seasons_scope_shows_its_unit_and_never_a_series_fallback(app, client, conn):
    library = seed_library(conn, '4', 'Shows')
    series = seed_item(conn, library, '41', 'Foundation', item_type='series')
    seed_item(conn, library, '41-s2', 'Season 2', item_type='season',
              parent_rating_key='41', season_number=2)
    seed_episode(conn, '41', 2, 1)
    conn.commit()
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'],
                             clock=lambda: NOW, collections=FakeCollections())
    engine.mark_for_removal(make_candidate(series, '41', 'Foundation S1',
                                           unit=Unit.SEASONS, seasons=(1,)))

    row = engine.open_requests()[0]
    assert row.unit is Unit.SEASONS and row.seasons == (1,)
    page = client.get('/?tab=leaving_soon').get_data(as_text=True)
    assert 'Foundation S1' in page
