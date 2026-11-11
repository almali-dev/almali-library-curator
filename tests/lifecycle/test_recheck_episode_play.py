"""An episode play arriving after a seasons mark cancels its removal (S6)."""

from datetime import datetime, timedelta, timezone

import pytest

from lifecycle_helpers import (
    FakeCollections,
    FrozenClock,
    StillMatches,
    default_settings,
    make_candidate,
    seed_episode,
    seed_item,
    seed_library,
    seed_play,
    seed_user,
)

from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine, RemovalState


@pytest.mark.parametrize(
    "clock_start",
    [
        pytest.param(None, id="frozen-at-noon"),
        pytest.param(
            datetime(2026, 11, 2, 23, 59, 59, tzinfo=timezone.utc),
            id="one-second-play-crosses-midnight",
        ),
    ],
)
@pytest.mark.parametrize(
    'played_season, seconds_after_mark, excluded, cancelled',
    [
        (2, 1, False, True),
        (3, 1, False, True),
        (1, 1, False, False),
        (2, -1, False, False),
        (2, 0, False, False),
        (2, 1, True, False),
    ],
)
def test_episode_play_arriving_after_marking_is_rechecked_in_the_seasons_scope(
    conn, played_season, seconds_after_mark, excluded, cancelled, clock_start
):
    clock = FrozenClock(clock_start) if clock_start is not None else FrozenClock()
    settings = default_settings(locale='en')
    collections = FakeCollections()
    rematcher = StillMatches()
    library_id = seed_library(conn, '2', 'Shows', section_type='show')
    series_id = seed_item(conn, library_id, '21', 'Lost in Space', item_type='series')
    for season in (1, 2, 3):
        seed_item(
            conn, library_id, f'21-s{season}', f'Season {season}', item_type='season',
            parent_rating_key='21', season_number=season,
        )
        seed_episode(conn, '21', season, 1)
    user_id = seed_user(conn, '1', 'Marc', excluded=excluded)
    candidate = make_candidate(
        series_id, '21', 'Lost in Space', unit=Unit.SEASONS, seasons=(2, 3)
    )
    # No protector: cancellation must come from the final activity re-check,
    # not protection or a rematch that no longer finds a candidate.
    engine = LifecycleEngine(
        conn, settings=settings, clock=clock, collections=collections, rematcher=rematcher
    )
    request = engine.mark_for_removal(candidate, grace_days=3)
    assert request.state is RemovalState.MARKED
    assert request.collection_added is True
    assert set(request.collection_members) == {'21-s2', '21-s3'}
    assert collections.holds('21-s2') and collections.holds('21-s3')
    assert not collections.holds('21-s1') and not collections.holds('21')
    assert conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 0

    # Insert only AFTER marking: a later sync can bring this episode event
    # into the database while the request is in its grace period.
    played_at = request.marked_at + timedelta(seconds=seconds_after_mark)
    clock.advance(days=1)
    seed_play(conn, user_id, f'21-s{played_season}e1', played_at, watched=False)
    early = engine.run_recheck()
    assert early.outcomes == () and early.errors == ()
    assert engine.request_for(candidate).state is RemovalState.MARKED
    assert collections.holds('21-s2') and collections.holds('21-s3')
    assert conn.execute('SELECT COUNT(*) FROM removal_events').fetchone()[0] == 0
    clock.advance(days=2)

    report = engine.run_recheck()

    assert report.errors == ()
    assert len(report.outcomes) == 1
    assert rematcher.calls == [series_id]
    outcome = report.outcomes[0]
    row = conn.execute(
        'SELECT state, recheck_status, cancel_reason, collection_added, deleted_at'
        ' FROM removal_requests WHERE id = ?', (request.id,),
    ).fetchone()
    events = conn.execute('SELECT * FROM removal_events').fetchall()
    if cancelled:
        # S6.2: the rendered day follows the frozen clock, not a literal: a
        # play one second after a late-evening mark lands on the next day.
        day = f"{played_at.day} {played_at.strftime('%B')}"
        reason = f"Removal cancelled: Marc started watching on {day}"
        assert report.passed == () and report.cancelled == (outcome,)
        assert outcome.passed is False
        assert outcome.exit_code.value == 'started_watching'
        assert outcome.cancel_reason == reason
        assert outcome.request.state is RemovalState.CANCELLED
        assert outcome.request.unit is Unit.SEASONS
        assert outcome.request.seasons == (2, 3)
        assert outcome.request.collection_members == ()
        assert outcome.request.collection_added is False
        assert tuple(row) == ('cancelled', 'started_watching', reason, 0, None)
        assert engine.request_for(candidate) is None
        assert engine.open_requests() == []
        assert not collections.holds('21-s2') and not collections.holds('21-s3')
        assert len(events) == 1
        event = events[0]
        assert event['removal_request_id'] == request.id
        assert event['media_item_id'] == series_id
        assert event['outcome'] == 'cancelled'
        assert event['title'] == 'Lost in Space S2–S3'
        assert event['unit'] == 'seasons'
        assert event['category'] == request.category.value
        assert event['reason'] == reason
        assert event['marked_by'] == request.marked_by
        assert event['occurred_at'] == clock().isoformat()
        assert event['freed_bytes'] in (0, None)
        assert event['error'] is None
        again = engine.run_recheck()
        assert again.outcomes == () and again.errors == ()
        assert conn.execute('SELECT COUNT(*) FROM removal_events').fetchone()[0] == 1
    else:
        assert report.cancelled == () and report.passed == (outcome,)
        assert outcome.passed is True
        assert outcome.exit_code is None and outcome.cancel_reason is None
        assert outcome.request.state is RemovalState.RECHECK_PASSED
        assert tuple(row) == ('recheck_passed', 'passed', None, 1, None)
        assert engine.request_for(candidate).id == request.id
        assert collections.holds('21-s2') and collections.holds('21-s3')
        assert events == []
    assert conn.execute('SELECT COUNT(*) FROM episodes').fetchone()[0] == 3
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 4
