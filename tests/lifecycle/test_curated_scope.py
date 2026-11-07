"""S14 rules 4 and 6 at the engine level: the scope gates detection,
marking and deletion. Items of deselected libraries stay in the database
but are never detected, marked or deleted.
"""

from __future__ import annotations

import pytest

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.models import CancelCode, RemovalState, UncuratedItemError
from lifecycle_helpers import (
    NOW,
    FrozenClock,
    default_settings,
    make_candidate,
    seed_item,
    seed_library,
)


def seed_two_libraries(conn):
    movies = seed_library(conn, "1", "Movies")
    vault = seed_library(conn, "2", "Filmoteca")
    tenet = seed_item(conn, movies, "11", "Tenet")
    heat = seed_item(conn, vault, "12", "Heat")
    return tenet, heat


def test_detection_never_reports_items_of_deselected_libraries(conn):
    tenet, heat = seed_two_libraries(conn)

    scoped = DetectionEngine(
        conn, clock=lambda: NOW, settings=default_settings(curated_libraries=("1",))
    ).scan()
    assert [candidate.media_item_id for candidate in scoped] == [tenet]

    # With no selection everything is detected exactly as before (S14 rule 1).
    unscoped = DetectionEngine(
        conn, clock=lambda: NOW, settings=default_settings()
    ).scan()
    assert {candidate.media_item_id for candidate in unscoped} == {tenet, heat}


def test_marking_an_item_of_an_uncurated_library_changes_nothing(conn):
    _, heat = seed_two_libraries(conn)
    engine = LifecycleEngine(
        conn, settings=default_settings(curated_libraries=("1",)), clock=lambda: NOW
    )

    with pytest.raises(UncuratedItemError):
        engine.mark_for_removal(make_candidate(heat, "12", "Heat"), marked_by="owner")

    assert conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_a_request_of_a_deselected_library_is_cancelled_not_curated(conn):
    _, heat = seed_two_libraries(conn)
    clock = FrozenClock()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=clock)
    engine.mark_for_removal(make_candidate(heat, "12", "Heat"), marked_by="owner")

    clock.advance(days=20)
    scoped = LifecycleEngine(
        conn, settings=default_settings(curated_libraries=("1",)), clock=clock
    )
    report = scoped.run_recheck()

    assert [outcome.exit_code for outcome in report.outcomes] == [CancelCode.NOT_CURATED]
    assert not report.passed
    row = conn.execute(
        "SELECT state, recheck_status, cancel_reason FROM removal_requests"
    ).fetchone()
    assert row["state"] == RemovalState.CANCELLED.value
    assert row["recheck_status"] == "not_curated"
    assert "no longer curated" in row["cancel_reason"]
    # Cancelled, never deleted: the request survives and history keeps it.
    assert conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0] == 1
    event = conn.execute("SELECT outcome FROM removal_events").fetchone()
    assert event["outcome"] == "cancelled"


def test_the_recheck_still_deletes_inside_the_scope_and_ignores_the_rule_unrestricted(conn):
    tenet, _ = seed_two_libraries(conn)
    clock = FrozenClock()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=clock)
    engine.mark_for_removal(make_candidate(tenet, "11", "Tenet"), marked_by="owner")

    clock.advance(days=20)
    # An empty selection curates every library, so the request passes as today.
    report = engine.run_recheck()

    assert [outcome.passed for outcome in report.outcomes] == [True]
    assert conn.execute("SELECT state FROM removal_requests").fetchone()["state"] == (
        RemovalState.RECHECK_PASSED.value
    )
