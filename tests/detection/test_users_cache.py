"""A reused engine refreshes users once per detection pass (S5)."""

import pytest

from fake_plex_server import FakePlexDataset, movie
from scenarios import NOW, default_settings, full_play, sync_dataset, ts

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.models import Category
from almali_library_curator.protection import ProtectionEngine


def run_pass(engine, method, item_ids):
    statements = []
    engine.conn.set_trace_callback(statements.append)
    try:
        if method == 'scan_items':
            result = engine.scan_items(item_ids).candidates
        elif method == 'scan_with_protection':
            result = engine.scan_with_protection().candidates
        else:
            result = engine.scan()
    finally:
        engine.conn.set_trace_callback(None)
    user_reads = sum('FROM users' in statement for statement in statements)
    return {candidate.plex_rating_key: candidate for candidate in result}, user_reads


def assert_never_watched(candidate, user_count):
    assert candidate.categories == (Category.NEVER_WATCHED,)
    assert candidate.facts['matches']['never_watched'] == {
        'added_at': '2024-04-01T12:00:00+00:00',
        'user_count': user_count,
    }


@pytest.mark.parametrize('with_protector', [False, True])
@pytest.mark.parametrize('method', ['scan', 'scan_with_protection', 'scan_items'])
@pytest.mark.parametrize('change', ['add', 'exclude', 'include', 'rename'])
def test_reused_engine_observes_changed_users_on_the_next_pass(
    conn, method, change, with_protector
):
    data = FakePlexDataset()
    data.accounts = [{'id': 1, 'name': 'Marc'}]
    data.items['1'] = [
        movie(str(100 + index), f'Film {index}', addedAt=ts(2024, 4, 1))
        for index in range(5)
    ] + [movie('200', 'Watched film', addedAt=ts(2024, 4, 1))]
    data.history = [full_play('200', 1, ts(2024, 5, 1))]
    sync_dataset(conn, data)
    if change == 'include':
        conn.execute("UPDATE users SET excluded = 1 WHERE title = 'Marc'")
        conn.commit()
    settings = default_settings()
    engine = DetectionEngine(
        conn, clock=lambda: NOW, settings=settings,
        protector=ProtectionEngine(conn, settings=settings) if with_protector else None,
    )
    item_ids = [row['id'] for row in conn.execute('SELECT id FROM media_items')]

    first, first_user_reads = run_pass(engine, method, item_ids)

    assert first_user_reads == 1
    assert set(first) == {'100', '101', '102', '103', '104', '200'}
    for key in ('100', '101', '102', '103', '104'):
        assert_never_watched(first[key], 0 if change == 'include' else 1)
    if change == 'include':
        assert_never_watched(first['200'], 0)
    else:
        assert first['200'].categories == (Category.MOVIES_NOT_REWATCHED,)
        assert first['200'].facts['matches']['movies_not_rewatched']['watchers'] == ['Marc']

    if change == 'add':
        cursor = conn.execute(
            "INSERT INTO users (plex_user_id, title) VALUES ('2', 'Anna')"
        )
        # A newly included account's activity must be attributed too, not
        # merely counted in the never-watched sentence.
        conn.execute(
            "INSERT INTO play_events (user_id, plex_rating_key, watched, played_at,"
            " position_ms, duration_ms, source) VALUES (?, '100', 1, ?, 0, 0, 'plex')",
            (cursor.lastrowid, NOW.isoformat()),
        )
    elif change == 'rename':
        conn.execute("UPDATE users SET title = 'Anna' WHERE title = 'Marc'")
    else:
        conn.execute(
            "UPDATE users SET excluded = ? WHERE title = 'Marc'",
            (1 if change == 'exclude' else 0,),
        )
    conn.commit()

    second, second_user_reads = run_pass(engine, method, item_ids)

    expected_keys = set(first) - {'100'} if change == 'add' else set(first)
    assert set(second) == expected_keys
    expected_count = 2 if change == 'add' else 0 if change == 'exclude' else 1
    for key in expected_keys - {'200'}:
        assert_never_watched(second[key], expected_count)
        if change != 'rename':
            assert second[key].reasons != first[key].reasons
    if change == 'exclude':
        assert_never_watched(second['200'], 0)
    else:
        watched = second['200']
        assert watched.categories == (Category.MOVIES_NOT_REWATCHED,)
        name = 'Anna' if change == 'rename' else 'Marc'
        assert watched.facts['matches']['movies_not_rewatched'] == {
            'watch_count': 1,
            'watchers': [name],
            'last_played_at': '2024-05-01T12:00:00+00:00',
        }
        assert name in watched.reasons[0]
        if change == 'rename':
            assert 'Marc' not in watched.reasons[0]
    assert second_user_reads == 1
