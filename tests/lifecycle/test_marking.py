"""S5 step 2 — marking a candidate for removal.

Every rule runs through the public interface: a real migrated database,
a frozen clock, the real ProtectionEngine and a recording collections
port.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from lifecycle_helpers import FakeCollections, FrozenClock, default_settings, make_candidate, seed_item, seed_library

from almali_library_curator.detection.models import Category, Unit
from almali_library_curator.lifecycle import (
    DuplicateMarkError,
    GraceRangeError,
    LifecycleEngine,
    ProtectedItemError,
    RemovalState,
)
from almali_library_curator.protection import ProtectionEngine


@pytest.fixture()
def settings():
    return default_settings()


@pytest.fixture()
def clock():
    return FrozenClock()


@pytest.fixture()
def collections():
    return FakeCollections()


@pytest.fixture()
def engine(conn, settings, clock, collections):
    protection = ProtectionEngine(conn, settings=settings)
    return LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=collections,
        protection=protection,
    )


@pytest.fixture()
def seeded(conn):
    library_id = seed_library(conn, "1", "Movies")
    item_id = seed_item(conn, library_id, "11", "Tenet", rating=7.3)
    return make_candidate(item_id, "11", "Tenet")


def test_mark_creates_the_request_with_the_default_grace(conn, engine, clock, collections, seeded):
    request = engine.mark_for_removal(seeded)

    assert request.state is RemovalState.MARKED
    assert request.grace_days == 14
    assert request.marked_at == clock.now
    assert request.marked_by == "owner"
    assert request.due_at == clock.now + timedelta(days=14)
    assert request.collection_added is True
    assert request.recheck_status is None
    assert request.cancel_reason is None

    # The item sits in the Leaving soon collection.
    assert collections.holds("11")

    # The candidates row is the scope carrier T7 will read back.
    row = conn.execute("SELECT * FROM candidates").fetchone()
    assert row["media_item_id"] == seeded.media_item_id
    assert row["category"] == Category.NEVER_WATCHED.value
    facts = json.loads(row["facts"])
    assert facts["unit"] == "movie"
    assert facts["seasons"] == []
    assert facts["matches"] == {"never_watched": {"example": "fact"}}

    request_row = conn.execute("SELECT * FROM removal_requests").fetchone()
    assert request_row["state"] == "marked"
    assert request_row["grace_days"] == 14
    assert request_row["collection_added"] == 1


def test_the_grace_default_comes_from_the_settings(conn, clock, collections, seeded):
    settings = default_settings(grace_days=30)
    engine = LifecycleEngine(
        conn, settings=settings, clock=clock, collections=collections
    )

    request = engine.mark_for_removal(seeded)

    assert request.grace_days == 30
    assert request.due_at == clock.now + timedelta(days=30)


@pytest.mark.parametrize("days", [3, 60])
def test_the_grace_bounds_are_accepted(conn, engine, clock, seeded, days):
    request = engine.mark_for_removal(seeded, grace_days=days)

    assert request.grace_days == days
    assert request.due_at == clock.now + timedelta(days=days)


@pytest.mark.parametrize("days", [2, 61])
def test_grace_out_of_range_refuses_and_persists_nothing(conn, engine, seeded, days):
    with pytest.raises(GraceRangeError):
        engine.mark_for_removal(seeded, grace_days=days)

    assert conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_marking_the_same_scope_twice_refuses_and_leaves_the_original(
    conn, engine, seeded
):
    first = engine.mark_for_removal(seeded, grace_days=7)

    with pytest.raises(DuplicateMarkError):
        engine.mark_for_removal(seeded)

    rows = conn.execute("SELECT * FROM removal_requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first.id
    assert rows[0]["due_at"] == first.due_at.isoformat()
    assert rows[0]["state"] == RemovalState.MARKED.value
    # The refused second mark persisted no extra candidates row either.
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_a_different_season_scope_of_the_same_series_is_a_new_request(conn, engine):
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    from lifecycle_helpers import seed_episode, seed_item

    series_id = seed_item(
        conn, library_id, "21", "Lost in Space", item_type="series"
    )
    seed_episode(conn, "21", 1, 1)
    seed_episode(conn, "21", 2, 1)
    season_one = make_candidate(
        series_id, "21", "Lost in Space S1", unit=Unit.SEASONS, seasons=(1,)
    )
    season_two = make_candidate(
        series_id, "21", "Lost in Space S2", unit=Unit.SEASONS, seasons=(2,)
    )

    first = engine.mark_for_removal(season_one)
    second = engine.mark_for_removal(season_two)

    assert first.id != second.id
    assert engine.request_for(season_one).id == first.id
    assert engine.request_for(season_two).id == second.id


def test_a_protected_candidate_is_refused(conn, engine, seeded):
    protected = make_candidate(
        seeded.media_item_id, "11", "Tenet", rating=8.7
    )

    with pytest.raises(ProtectedItemError):
        engine.mark_for_removal(protected)

    assert conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
