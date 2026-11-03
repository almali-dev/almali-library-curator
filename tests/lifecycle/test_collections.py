"""S5 step 3 — the Leaving soon collection.

The collection is created only when missing, carries the configured
name, and only ever holds marked items: a cancelled item is out.
"""

from __future__ import annotations

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
