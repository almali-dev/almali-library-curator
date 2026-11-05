"""S14 rules 4-7 through the screens: the curated scope, the checkboxes,
the refusals, and the history ledger that stays global.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from markupsafe import Markup

from almali_library_curator.config import save_settings
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import make_candidate, seed_item, seed_library

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1_000_000_000


def row(identifier, title, size, **extra):
    return dict(media_item_id=identifier, title=title, unit='movie', seasons=[],
                type_badge='MOVIE', categories=['never_watched'], reasons=['A reason.'],
                reason='A reason.', size_bytes=size, disk='disk3', rating=7.4, rating_source='IMDb', **extra)


def snapshot(app, candidates, protected=()):
    settings = replace(app.config['SETTINGS'], dry_run=False)
    save_settings(settings)

    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=len(candidates), candidate_rows=list(candidates),
                                          protected_rows=list(protected), protected=len(protected))}
    return ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')


def text(response):
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def seed_two_libraries(conn):
    movies = seed_library(conn, '1', 'Movies')
    vault = seed_library(conn, '2', 'Filmoteca')
    tenet = seed_item(conn, movies, '11', 'Tenet')
    heat = seed_item(conn, vault, '12', 'Heat')
    return tenet, heat


def curate(app, *keys):
    save_settings(replace(app.config['SETTINGS'], curated_libraries=tuple(keys)))


def checkbox(html, name, value):
    return re.search(
        rf'<input type="checkbox" name="{name}" value="{value}"[^>]*>', html)


# -- rule 7: the curated checkbox next to the protected one -------------------


def test_the_wizard_renders_both_checkboxes_checked_by_default(client, conn):
    seed_two_libraries(conn)
    html = client.get('/setup?step=libraries').get_data(as_text=True)
    for key in ('1', '2'):
        curated = checkbox(html, 'curated_libraries', key)
        assert curated and 'checked' in curated.group(0), key
        assert checkbox(html, 'protected_libraries', key), key
        assert 'disabled' not in checkbox(html, 'protected_libraries', key).group(0)
    assert 'Filmoteca' in text(client.get('/setup?step=libraries'))


def test_the_settings_screen_renders_both_checkboxes(client, conn):
    seed_two_libraries(conn)
    html = client.get('/settings').get_data(as_text=True)
    for key in ('1', '2'):
        assert checkbox(html, 'curated_libraries', key), key
        assert checkbox(html, 'protected_libraries', key), key


def test_the_protected_checkbox_is_disabled_while_the_library_is_not_curated(app, client, conn):
    seed_two_libraries(conn)
    curate(app, '1')
    html = client.get('/settings').get_data(as_text=True)
    assert 'disabled' in checkbox(html, 'protected_libraries', '2').group(0)
    assert 'disabled' not in checkbox(html, 'protected_libraries', '1').group(0)
    # The wizard shows the same pair.
    setup = client.get('/setup?step=libraries').get_data(as_text=True)
    assert 'disabled' in checkbox(setup, 'protected_libraries', '2').group(0)
    assert 'checked' not in checkbox(setup, 'curated_libraries', '2').group(0)


def test_saving_the_selection_through_the_settings_post_persists_it(app, client, conn):
    seed_two_libraries(conn)
    response = client.post('/settings', data={'curated_libraries': '1'})
    assert response.status_code == 303
    with app.test_request_context('/'):
        from almali_library_curator.web.resources import get_settings
        assert get_settings().curated_libraries == ('1',)
    saved = json.loads((app.config['SETTINGS'].config_dir / 'settings.json').read_text())
    assert saved['curated_libraries'] == ['1']


# -- rules 4 and 5: the screens follow the scope, the history does not ---------


def test_the_screens_count_only_curated_libraries(app, client, conn):
    tenet, heat = seed_two_libraries(conn)
    snapshot(app, [row(tenet, 'Tenet', 2 * GB), row(heat, 'Heat', GB)])
    curate(app, '1')

    body = text(client.get('/'))
    assert 'Tenet' in body and 'Heat' not in body
    assert 'Candidates · 1' in body and 'Candidates · 2' not in body


def test_a_deselected_library_disappears_from_candidates_and_leaving_soon(app, client, conn):
    tenet, heat = seed_two_libraries(conn)
    snapshot(app, [row(tenet, 'Tenet', 2 * GB), row(heat, 'Heat', GB)])
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: NOW)
    engine.mark_for_removal(make_candidate(tenet, '11', 'Tenet'), marked_by='owner')
    engine.mark_for_removal(make_candidate(heat, '12', 'Heat'), marked_by='owner')
    curate(app, '1')

    leaving = text(client.get('/?tab=leaving_soon'))
    assert 'Heat' not in leaving and 'Tenet' in leaving
    assert 'Heat' not in text(client.get('/'))


def test_history_and_freed_space_stay_global(app, client, conn):
    seed_two_libraries(conn)
    curate(app, '1')
    conn.execute(
        'INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes, disk,'
        " reason, marked_by, occurred_at) VALUES ('deleted', 'Heat', 'movie', ?, ?, 'disk3',"
        " 'A reason', 'owner', '2026-07-01T12:00:00+00:00')", (2 * GB, 2 * GB))
    conn.commit()

    body = text(client.get('/history'))
    assert 'Heat' in body
    assert 'Freed so far 2 GB' in text(client.get('/'))


def test_the_sources_panel_counts_only_curated_disks(app, client, conn):
    tenet, heat = seed_two_libraries(conn)
    conn.execute('UPDATE media_items SET disk = ? WHERE id = ?', ('disk1', tenet))
    conn.execute('UPDATE media_items SET disk = ? WHERE id = ?', ('disk9', heat))
    conn.commit()

    assert 'Storage · 2 disks' in text(client.get('/'))
    curate(app, '1')
    assert 'Storage · 1 disk' in text(client.get('/'))


# -- rule 6: nothing may be marked outside the scope ---------------------------


def test_marking_an_uncurated_item_is_refused(app, client, conn):
    tenet, heat = seed_two_libraries(conn)
    snapshot(app, [row(tenet, 'Tenet', GB), row(heat, 'Heat', GB)])
    curate(app, '1')

    response = client.post(f'/items/{heat}/mark', data={'unit': 'movie'})
    assert response.status_code == 303
    with client.session_transaction() as session:
        assert session['_flashes'][-1][1] == 'web.flash.not_curated'
    assert conn.execute('SELECT COUNT(*) FROM removal_requests').fetchone()[0] == 0


# -- rule 7: a protected library that is not curated is refused ----------------


def test_protecting_an_uncurated_library_is_refused(app, client, conn):
    seed_two_libraries(conn)
    path = app.config['SETTINGS'].config_dir / 'settings.json'
    before = path.read_bytes()

    response = client.post('/settings?lang=en', data={
        'curated_libraries': '1', 'protected_libraries': '2'})

    assert response.status_code == 400
    assert path.read_bytes() == before
    assert 'A protected library must also be curated.' in text(response)


def test_protecting_by_title_an_uncurated_library_is_refused_too(app, client, conn):
    seed_two_libraries(conn)
    path = app.config['SETTINGS'].config_dir / 'settings.json'
    before = path.read_bytes()

    response = client.post('/settings?lang=en', data={
        'curated_libraries': '1', 'protected_libraries': 'filmoteca'})

    assert response.status_code == 400
    assert path.read_bytes() == before


def test_a_curated_library_still_accepts_its_protection(app, client, conn):
    seed_two_libraries(conn)
    response = client.post('/settings', data={
        'curated_libraries': '1', 'protected_libraries': '1'})
    assert response.status_code == 303
    with app.test_request_context('/'):
        from almali_library_curator.web.resources import get_settings
        loaded = get_settings()
    assert loaded.curated_libraries == ('1',)
    assert loaded.protected_libraries == ('1',)
