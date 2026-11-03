"""S5 steps 2 and 4 — undo and Keep it cancel a removal.

Both send the item back to the candidates and take it out of the
Leaving soon collection, each with its own localized reason and a
cancellation record in history.
"""

from __future__ import annotations

import pytest

from lifecycle_helpers import FakeCollections, FrozenClock, default_settings, make_candidate, seed_item, seed_library

from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import (
    LifecycleEngine,
    RemovalState,
    RequestStateError,
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
    return LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=collections,
        protection=ProtectionEngine(conn, settings=settings),
    )


@pytest.fixture()
def seeded(conn):
    library_id = seed_library(conn, "1", "Movies")
    item_id = seed_item(conn, library_id, "11", "Tenet", rating=7.3)
    return make_candidate(item_id, "11", "Tenet")


def test_undo_cancels_with_the_localized_reason_and_leaves_the_collection(
    conn, engine, collections, seeded
):
    request = engine.mark_for_removal(seeded)
    assert collections.holds("11")

    cancelled = engine.undo(request)

    assert cancelled.state is RemovalState.CANCELLED
    assert cancelled.cancel_reason == translate("removal.cancel.undo", "en")
    assert cancelled.recheck_status == "undo"
    assert cancelled.collection_added is False
    assert not collections.holds("11")

    # The cancellation is recorded in history (S5 step 4, the records T7 extends).
    row = conn.execute("SELECT * FROM removal_events").fetchone()
    assert row["outcome"] == "cancelled"
    assert row["title"] == "Tenet"
    assert row["unit"] == "movie"
    assert row["reason"] == cancelled.cancel_reason
    assert row["marked_by"] == "owner"
    assert row["removal_request_id"] == cancelled.id


def test_keep_it_cancels_with_its_own_distinct_reason(
    conn, engine, collections, seeded
):
    request = engine.mark_for_removal(seeded)

    cancelled = engine.keep_it(request)

    assert cancelled.state is RemovalState.CANCELLED
    assert cancelled.cancel_reason == translate("removal.cancel.keep_it", "en")
    assert cancelled.recheck_status == "keep_it"
    assert (
        cancelled.cancel_reason
        != translate("removal.cancel.undo", "en")
    )
    assert not collections.holds("11")


def test_undoing_an_already_cancelled_request_is_refused(conn, engine, seeded):
    request = engine.mark_for_removal(seeded)
    engine.undo(request)

    with pytest.raises(RequestStateError):
        engine.undo(request)

    assert conn.execute("SELECT COUNT(*) FROM removal_events").fetchone()[0] == 1


def test_undo_is_possible_until_deletion_even_after_the_recheck_passed(
    conn, engine, clock, collections, seeded
):
    """S5 step 2: undo is possible until deletion, so a request whose
    re-check passed can still be taken back."""
    request = engine.mark_for_removal(seeded, grace_days=3)
    clock.advance(days=4)
    engine.run_recheck()
    assert engine.open_requests()[0].state is RemovalState.RECHECK_PASSED

    cancelled = engine.undo(request)

    assert cancelled.state is RemovalState.CANCELLED
    assert not collections.holds("11")
