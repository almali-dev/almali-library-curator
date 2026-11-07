"""S5 step 3 — the Leaving soon collection.

The collection is created only when missing, carries the configured
name, and only ever holds marked items: a cancelled item is out.
"""

from __future__ import annotations

import json

import pytest

from lifecycle_helpers import FakeCollections, FrozenClock, default_settings, make_candidate, seed_item, seed_library

from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine, RemovalState
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
    return LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=collections,
        protection=ProtectionEngine(conn, settings=settings),
    )


@pytest.fixture()
def movies(conn):
    library_id = seed_library(conn, "1", "Movies")
    first = seed_item(conn, library_id, "11", "Tenet", rating=7.3)
    second = seed_item(conn, library_id, "12", "Heat", rating=7.3)
    return (
        make_candidate(first, "11", "Tenet"),
        make_candidate(second, "12", "Heat"),
    )


def test_the_collection_is_created_only_when_missing_and_holds_only_marked_items(
    conn, engine, clock, collections, movies
):
    tenet, heat = movies

    engine.mark_for_removal(tenet)
    engine.mark_for_removal(heat)

    # One collection, one create, both marked items inside it.
    assert collections.creates == 1
    assert collections.holds("11")
    assert collections.holds("12")
    assert {name for _, name in collections.ensure_calls} == {"Leaving soon"}

    engine.undo(engine.request_for(tenet))

    assert not collections.holds("11")
    assert collections.holds("12")


def test_the_collection_follows_its_configured_name(
    conn, clock, collections, movies
):
    settings = default_settings(collection_name="Surt aviat")
    engine = LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=collections,
        protection=ProtectionEngine(conn, settings=settings),
    )
    engine.mark_for_removal(movies[0])

    assert collections.ensure_calls == [("1", "Surt aviat")]
    assert collections.holds("11")


def test_a_cancelled_item_is_no_longer_in_the_collection(conn, engine, collections, movies):
    tenet, _ = movies
    request = engine.mark_for_removal(tenet)

    cancelled = engine.keep_it(request)

    assert cancelled.state is RemovalState.CANCELLED
    assert not collections.holds("11")


def test_a_partial_collection_write_keeps_the_members_it_already_added(
    conn, engine, collections
):
    """A member that made it into the collection is tracked even when the
    next one fails, so no cleanup skips it."""
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    series = seed_item(conn, library_id, "700", "Lost in Space", item_type="series")
    for number in (2, 3):
        seed_item(
            conn, library_id, f"700-s{number}", f"Season {number}",
            item_type="season", parent_rating_key="700", season_number=number,
        )
    scope = make_candidate(
        series, "700", "Lost in Space S2–S3", unit=Unit.SEASONS, seasons=(2, 3)
    )
    collections.fail_add_after = 1

    with pytest.raises(RuntimeError):
        engine.mark_for_removal(scope)

    request = engine.open_requests()[0]
    assert request.collection_added is True
    facts = json.loads(
        conn.execute(
            "SELECT facts FROM candidates WHERE id = ?", (request.candidate_id,)
        ).fetchone()["facts"]
    )
    assert facts["collection_members"] == ["700-s2"]
    assert collections.holds("700-s2")

    engine.undo(request)

    assert not collections.holds("700-s2")


def test_the_recheck_run_adds_the_members_a_failed_mark_left_out(
    conn, engine, collections
):
    """A marked scope always ends up whole in Leaving soon: the next run
    adds the members a failed collection write left out (S5 step 3)."""
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    series = seed_item(conn, library_id, "700", "Lost in Space", item_type="series")
    for number in (2, 3):
        seed_item(
            conn, library_id, f"700-s{number}", f"Season {number}",
            item_type="season", parent_rating_key="700", season_number=number,
        )
    scope = make_candidate(
        series, "700", "Lost in Space S2–S3", unit=Unit.SEASONS, seasons=(2, 3)
    )
    collections.fail_add_after = 1
    with pytest.raises(RuntimeError):
        engine.mark_for_removal(scope)
    assert not collections.holds("700-s3")
    collections.fail_add_after = None

    report = engine.run_recheck()

    assert report.passed == () and report.cancelled == ()
    assert collections.holds("700-s2") and collections.holds("700-s3")
    request = engine.open_requests()[0]
    assert sorted(request.collection_members) == ["700-s2", "700-s3"]
    assert request.state is RemovalState.MARKED


def test_two_season_scopes_of_one_series_keep_their_own_membership(
    conn, engine, collections
):
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    series = seed_item(conn, library_id, "700", "Lost in Space", item_type="series")
    for number in (2, 3):
        seed_item(
            conn,
            library_id,
            f"700-s{number}",
            f"Lost in Space S{number}",
            item_type="season",
            parent_rating_key="700",
            season_number=number,
        )
    season_two = make_candidate(series, "700", "S2", unit=Unit.SEASONS, seasons=(2,))
    season_three = make_candidate(series, "700", "S3", unit=Unit.SEASONS, seasons=(3,))

    engine.mark_for_removal(season_two)
    engine.mark_for_removal(season_three)
    engine.undo(engine.request_for(season_two))

    # The season scope that is still open keeps its own membership.
    assert not collections.holds("700-s2")
    assert collections.holds("700-s3")
