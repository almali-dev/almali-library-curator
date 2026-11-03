"""T7 state integration with the existing T6 public interface."""

import pytest

from almali_library_curator.lifecycle import LifecycleEngine, RequestStateError
from lifecycle_helpers import (
    FakeCollections, FrozenClock, make_candidate, default_settings,
    seed_item, seed_library, seed_play, seed_user,
)


def scenario(conn):
    clock = FrozenClock()
    collections = FakeCollections()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=clock,
                             collections=collections)
    library = seed_library(conn, "movies", "Movies")
    item = seed_item(conn, library, "film", "Film")
    candidate = make_candidate(item, "film", "Film")
    request = engine.mark_for_removal(candidate)
    return engine, clock, collections, candidate, request


def set_state(conn, request, state):
    conn.execute("UPDATE removal_requests SET state = ? WHERE id = ?", (state, request.id))
    conn.commit()


def test_deleted_is_terminal_but_does_not_block_a_new_mark(conn):
    engine, _, collections, candidate, request = scenario(conn)
    set_state(conn, request, "deleted")
    assert engine.open_requests() == []
    assert engine.request_for(candidate) is None
    assert engine.retry_collection_cleanup() == 1
    assert not collections.holds("film")
    with pytest.raises(RequestStateError):
        engine.undo(request)
    assert engine.mark_for_removal(candidate).id != request.id


@pytest.mark.parametrize("action", ["undo", "keep_it"])
def test_failed_removal_can_restore_members_and_be_cancelled(conn, action):
    engine, _, collections, candidate, request = scenario(conn)
    engine.undo(request)
    collections.fail_add_after = collections.adds
    with pytest.raises(RuntimeError):
        engine.mark_for_removal(candidate)
    request = engine.request_for(candidate)
    set_state(conn, request, "removal_failed")
    collections.fail_add_after = None
    assert engine.retry_collection_additions() == 1
    assert collections.holds("film")
    assert getattr(engine, action)(engine.open_requests()[0]).state.value == "cancelled"
    assert not collections.holds("film")


@pytest.mark.parametrize("played", [False, True])
def test_failed_removal_must_pass_the_final_check_again(conn, played):
    engine, clock, collections, _, request = scenario(conn)
    set_state(conn, request, "removal_failed")
    clock.advance(days=14)
    if played:
        user = seed_user(conn, "1", "Marc")
        seed_play(conn, user, "film", clock())
    report = engine.run_recheck()
    assert len(report.outcomes) == 1
    assert report.outcomes[0].passed is not played
    assert report.outcomes[0].request.state.value == ("cancelled" if played else "recheck_passed")
    assert collections.holds("film") is not played
