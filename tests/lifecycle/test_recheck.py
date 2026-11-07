"""S5 step 4 — the final re-check on the last day of grace.

The three exits apply in order: no longer matching, newly protected,
someone started watching. No exit means the pass-through state and no
deletion at all.
"""

from __future__ import annotations

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
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine, RemovalState
from almali_library_curator.protection import ProtectionEngine

# The marking moment: plays after it cancel, plays before it do not.
MARKED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def clock():
    return FrozenClock(MARKED_AT)


@pytest.fixture()
def collections():
    return FakeCollections()


def build_engine(conn, clock, collections=None, *, settings=None, rematcher=None):
    settings = settings or default_settings()
    return LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=collections,
        protection=ProtectionEngine(conn, settings=settings),
        rematcher=rematcher,
    )


@pytest.fixture()
def movie(conn):
    library_id = seed_library(conn, "1", "Movies")
    item_id = seed_item(conn, library_id, "11", "Tenet", rating=7.3)
    return make_candidate(item_id, "11", "Tenet")


def test_recheck_before_due_changes_nothing(conn, clock, collections, movie):
    engine = build_engine(conn, clock, collections)
    request = engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=1)

    report = engine.run_recheck()

    assert report.outcomes == ()
    unchanged = engine.request_for(movie)
    assert unchanged.state is RemovalState.MARKED
    assert unchanged.recheck_status is None


def test_recheck_runs_on_the_due_day_itself(conn, clock, collections, movie):
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=3)

    report = engine.run_recheck()

    assert len(report.outcomes) == 1
    assert report.outcomes[0].passed is True


@pytest.mark.parametrize(
    "locale, expected",
    [
        (
            "en",
            "Removal cancelled: Marc started watching on 30 October",
        ),
        (
            "ca",
            "Eliminació cancel·lada: Marc va començar a mirar-la el 30 d'octubre.",
        ),
        (
            "es",
            "Eliminación cancelada: Marc empezó a verla el 30 de octubre.",
        ),
    ],
)
def test_a_play_after_marking_cancels_with_the_exact_message(
    conn, clock, collections, movie, locale, expected
):
    marc = seed_user(conn, "1", "Marc")
    seed_play(
        conn, marc, "11", datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    )
    engine = build_engine(
        conn, clock, collections, settings=default_settings(locale=locale)
    )
    request = engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.cancelled) == 1
    cancelled = report.cancelled[0]
    assert cancelled.exit_code.value == "started_watching"
    assert cancelled.cancel_reason == expected

    reloaded = engine.request_for(movie)
    assert reloaded is None  # cancelled requests are not open any more
    row = conn.execute(
        "SELECT state, cancel_reason, recheck_status FROM removal_requests WHERE id = ?",
        (request.id,),
    ).fetchone()
    assert row["state"] == RemovalState.CANCELLED.value
    assert row["cancel_reason"] == expected
    assert row["recheck_status"] == "started_watching"
    assert not collections.holds("11")


def test_a_play_before_marking_does_not_cancel(conn, clock, collections, movie):
    marc = seed_user(conn, "1", "Marc")
    seed_play(
        conn, marc, "11", datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    )
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.passed) == 1
    assert conn.execute(
        "SELECT state FROM removal_requests"
    ).fetchone()["state"] == RemovalState.RECHECK_PASSED.value


def test_an_excluded_users_play_does_not_cancel(conn, clock, collections, movie):
    guest = seed_user(conn, "9", "Guest", excluded=True)
    seed_play(
        conn, guest, "11", datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    )
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.passed) == 1


@pytest.fixture()
def seasons_candidate(conn):
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    series_id = seed_item(conn, library_id, "21", "Lost in Space", item_type="series")
    seed_episode(conn, "21", 1, 1)
    seed_episode(conn, "21", 2, 1)
    seed_episode(conn, "21", 3, 1)
    return make_candidate(
        series_id, "21", "Lost in Space S2–S3", unit=Unit.SEASONS, seasons=(2, 3)
    )


def test_a_play_outside_the_scoped_seasons_does_not_cancel(
    conn, clock, collections, seasons_candidate
):
    marc = seed_user(conn, "1", "Marc")
    seed_play(
        conn,
        marc,
        "21-s1e1",
        datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc),
    )
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(seasons_candidate, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.passed) == 1


def test_a_play_inside_the_scoped_seasons_cancels(
    conn, clock, collections, seasons_candidate
):
    marc = seed_user(conn, "1", "Marc")
    seed_play(
        conn,
        marc,
        "21-s2e1",
        datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc),
    )
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(seasons_candidate, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.cancelled) == 1
    assert report.cancelled[0].exit_code.value == "started_watching"


def test_a_play_recorded_against_the_series_also_cancels(
    conn, clock, collections, seasons_candidate
):
    # Some Plex history entries carry the show's own rating key. A play of
    # the show is a play of the season scope, so it must cancel the
    # removal: the safety net never gets weaker than it was.
    marc = seed_user(conn, "1", "Marc")
    seed_play(
        conn,
        marc,
        "21",
        datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc),
    )
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(seasons_candidate, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.cancelled) == 1
    assert report.cancelled[0].exit_code.value == "started_watching"


def test_a_protection_added_after_marking_cancels_at_recheck(
    conn, clock, collections, movie
):
    protection = ProtectionEngine(conn, settings=default_settings())
    engine = build_engine(conn, clock, collections)
    engine.mark_for_removal(movie, grace_days=3)
    protection.keep_forever(movie)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.cancelled) == 1
    cancelled = report.cancelled[0]
    assert cancelled.exit_code.value == "protected"
    assert cancelled.cancel_reason == translate("removal.cancel.protected", "en")
    assert not collections.holds("11")


def test_all_clear_reaches_the_pass_through_state_without_deleting(
    conn, clock, collections, movie
):
    engine = build_engine(
        conn, clock, collections, rematcher=StillMatches(answer=True)
    )
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=11)

    report = engine.run_recheck()

    assert len(report.passed) == 1
    assert report.passed[0].exit_code is None
    # The item STAYS in the collection; it leaves at deletion (T7).
    assert collections.holds("11")
    row = conn.execute("SELECT state, recheck_status FROM removal_requests").fetchone()
    assert row["state"] == RemovalState.RECHECK_PASSED.value
    assert row["recheck_status"] == "passed"
    # No deletion happened: no removal event at all, least of all a 'deleted' one.
    assert conn.execute("SELECT COUNT(*) FROM removal_events").fetchone()[0] == 0


def test_without_the_collections_port_everything_still_works(
    conn, clock, movie
):
    engine = build_engine(conn, clock, collections=None)
    request = engine.mark_for_removal(movie, grace_days=3)
    assert request.collection_added is False

    clock.advance(days=11)
    report = engine.run_recheck()

    assert len(report.passed) == 1
    reloaded = engine.open_requests()[0]
    assert reloaded.state is RemovalState.RECHECK_PASSED
    assert reloaded.collection_added is False


def test_a_user_excluded_by_name_in_the_settings_does_not_cancel(
    conn, clock, collections, movie
):
    """The owner excludes an account by name, not only by its flag."""
    guest = seed_user(conn, "9", "Guest")
    seed_play(conn, guest, "11", datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc))
    engine = build_engine(
        conn, clock, collections, settings=default_settings(excluded_users=("Guest",))
    )
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=11)

    assert len(engine.run_recheck().passed) == 1


@pytest.mark.parametrize(
    "still_matches, expected", [(False, "no_longer_matches"), (True, "protected")]
)
def test_the_exits_apply_in_the_specified_order(
    conn, clock, collections, movie, still_matches, expected
):
    """S5 step 4: no longer matches, then protected, then started watching."""
    marc = seed_user(conn, "1", "Marc")
    seed_play(conn, marc, "11", datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc))
    engine = build_engine(
        conn, clock, collections, rematcher=StillMatches(answer=still_matches)
    )
    engine.mark_for_removal(movie, grace_days=3)
    ProtectionEngine(conn, settings=default_settings()).keep_forever(movie)
    clock.advance(days=11)

    assert engine.run_recheck().cancelled[0].exit_code.value == expected


class CancelsConcurrently:
    """A rematcher that cancels the request while the re-check runs."""

    def __init__(self, conn):
        self.conn = conn

    def still_matches(self, candidate):
        self.conn.execute(
            "UPDATE removal_requests SET state = 'cancelled' WHERE state = 'marked'"
        )
        self.conn.commit()
        return True


def test_a_state_that_moved_underneath_the_engine_is_never_overwritten(
    conn, clock, collections, movie
):
    """A concurrent cancellation is not overwritten by a passing re-check."""
    engine = build_engine(
        conn, clock, collections, rematcher=CancelsConcurrently(conn)
    )
    engine.mark_for_removal(movie, grace_days=3)
    clock.advance(days=4)

    report = engine.run_recheck()

    state = conn.execute("SELECT state FROM removal_requests").fetchone()["state"]
    assert state == RemovalState.CANCELLED.value
    assert report.passed == ()


def test_a_failed_collection_removal_keeps_the_member_for_the_cleanup(
    conn, clock, collections, movie
):
    """The cancellation is durable before the removal, and a removal that
    fails keeps its member recorded so a later cleanup finishes it."""
    engine = build_engine(conn, clock, collections)
    request = engine.mark_for_removal(movie, grace_days=3)
    collections.fail_remove.add("11")

    cancelled = engine.undo(request)

    assert cancelled.state is RemovalState.CANCELLED
    assert cancelled.collection_added is True
    assert collections.holds("11")

    assert engine.retry_collection_cleanup() == 1
    assert not collections.holds("11")
    row = conn.execute(
        "SELECT collection_added FROM removal_requests WHERE id = ?", (request.id,)
    ).fetchone()
    assert row["collection_added"] == 0


def test_the_recheck_run_retries_the_pending_collection_cleanup(
    conn, clock, collections, movie
):
    """No lifecycle path may strand a member of a cancelled request."""
    engine = build_engine(conn, clock, collections)
    request = engine.mark_for_removal(movie, grace_days=3)
    collections.fail_remove.add("11")
    engine.undo(request)
    assert collections.holds("11")

    engine.run_recheck()

    assert not collections.holds("11")


def test_a_failing_request_does_not_starve_the_other_due_requests(conn, clock, caplog):
    library_id = seed_library(conn, "1", "Movies")
    first = seed_item(conn, library_id, "11", "Tenet")
    second = seed_item(conn, library_id, "12", "Heat")
    engine = build_engine(conn, clock)
    one = engine.mark_for_removal(make_candidate(first, "11", "Tenet"))
    two = engine.mark_for_removal(make_candidate(second, "12", "Heat"))

    # The first request loses its scope carrier and can no longer be read.
    conn.execute("DELETE FROM candidates WHERE id = ?", (one.candidate_id,))
    conn.commit()
    clock.advance(days=20)

    report = engine.run_recheck()

    # The broken request is reported and stays marked; the other one is
    # re-checked anyway instead of waiting behind it.
    assert [outcome.request.id for outcome in report.outcomes] == [two.id]
    assert report.outcomes[0].passed is True
    state = conn.execute(
        "SELECT state FROM removal_requests WHERE id = ?", (one.id,)
    ).fetchone()[0]
    assert state == "marked"
    assert "removal_recheck_failed" in caplog.text
