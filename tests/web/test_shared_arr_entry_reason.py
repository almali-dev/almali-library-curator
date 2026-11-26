"""#136: the sharing refusal names the other library on the card and History.

The stored failure is the translation key, never a rendered sentence (S16
rule 3), so both screens run the same read-only sharing check again and
pass the other library's name to the sentence in the reader's language.
"""

from __future__ import annotations

from datetime import datetime, timezone

from markupsafe import escape

from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine
from lifecycle_helpers import FakeCollections, FrozenClock, make_candidate, seed_item, seed_library

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
KEY = 'removal.failed.shared_arr_entry'


def two_libraries_one_radarr_movie(app, conn):
    """Library ``Movies`` and ``Movies 4K`` over the same Radarr movie."""
    movies = seed_library(conn, '1', 'Movies')
    fourk = seed_library(conn, '3', 'Movies 4K')
    first = seed_item(conn, movies, '21', 'Tenet')
    second = seed_item(conn, fourk, '22', 'Tenet')
    for item in (first, second):
        conn.execute('UPDATE media_items SET radarr_id=10, tmdb_id=2010 WHERE id=?', (item,))
    conn.commit()
    return first, second


def failed_request(app, conn, item):
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'],
                             clock=FrozenClock(NOW), collections=FakeCollections())
    request = engine.mark_for_removal(make_candidate(item, '21', 'Tenet'))
    conn.execute('UPDATE removal_requests SET state=?, last_error=? WHERE id=?',
                 ('removal_failed', KEY, request.id))
    conn.commit()
    return request


def test_the_card_names_the_other_library_of_a_sharing_refusal(app, client, conn):
    from almali_library_curator.web.views import leaving_soon_view

    first, second = two_libraries_one_radarr_movie(app, conn)
    request = failed_request(app, conn, first)
    row = leaving_soon_view(conn, app.config['SETTINGS'], clock=lambda: NOW).items[0]
    assert (row.id, row.error) == (request.id, KEY)
    assert row.error_params == {'library': 'Movies 4K'}
    page = client.get('/?tab=leaving_soon').get_data(as_text=True)
    sentence = translate(KEY, 'en').format(library='Movies 4K')
    assert str(escape(sentence)) in page


def test_history_names_the_other_library_of_a_sharing_refusal(app, client, conn):
    from almali_library_curator.web.views import history_view

    first, second = two_libraries_one_radarr_movie(app, conn)
    conn.execute(
        'INSERT INTO removal_events (media_item_id, outcome, title, unit, error,'
        " occurred_at) VALUES (?, 'failed', 'Tenet', 'movie', ?, ?)",
        (first, KEY, NOW.isoformat()))
    conn.commit()
    event = history_view(conn, locale='en').events[0]
    assert event.error == KEY
    assert event.error_params == {'library': 'Movies 4K'}
    page = client.get('/history').get_data(as_text=True)
    assert str(escape(translate(KEY, 'en').format(library='Movies 4K'))) in page


def test_a_history_row_without_a_named_library_still_reads_as_a_sentence(app, conn):
    # A row whose sharing rows are gone (or a scope the lookup cannot read)
    # keeps the stored key and the generic phrase, never a bare placeholder.
    from almali_library_curator.web.views import history_view

    conn.execute(
        'INSERT INTO removal_events (outcome, title, unit, error, occurred_at)'
        " VALUES ('failed', 'Tenet', 'movie', ?, ?)", (KEY, NOW.isoformat()))
    conn.commit()
    event = history_view(conn, locale='en').events[0]
    assert event.error_params == {'library': translate('removal.failed.shared_other_library', 'en')}
    assert '{library}' not in translate(KEY, 'en').format(**event.error_params)


def test_every_locale_has_the_sharing_refusal_wording():
    for locale in ('en', 'ca', 'es'):
        for code in ('shared_arr_entry', 'shared_file'):
            key = f'removal.failed.{code}'
            sentence = translate(key, locale)
            assert sentence != key
            named = sentence.format(library='Movies 4K')
            assert 'Movies 4K' in named and '{library}' not in named
        generic = translate('removal.failed.shared_other_library', locale)
        assert generic != 'removal.failed.shared_other_library'
