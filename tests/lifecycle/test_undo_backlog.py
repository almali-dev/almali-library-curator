"""#155 S1: an Undo cleans up its own request, and the scan finishes the backlog.

One gunicorn worker with four request threads serves every screen, so a click
may not walk every collection removal the database still owes. Against a hung
Plex each pending request costs its own bounded calls, so a backlog of three
made one Undo four requests deep.

The engine the web layer builds carries ``cleanup_backlog_on_cancel=False``:
it cleans the request it just cancelled and nothing else. The scan's own retry
-- which already sweeps the backlog on every re-check -- finishes what a failed
write left behind, and the engines the scan and the deletion executor build
keep their sweep.
"""

from __future__ import annotations

from almali_library_curator.lifecycle import LifecycleEngine, RemovalState
from lifecycle_helpers import (
    FakeCollections,
    FrozenClock,
    default_settings,
    make_candidate,
    seed_item,
    seed_library,
)


class CountedCollections(FakeCollections):
    """The fake collection port, plus every removal it was asked to make."""

    def __init__(self) -> None:
        super().__init__()
        self.removals: list[str] = []

    def remove_from_collection(self, collection_key: str, rating_key: str) -> None:
        self.removals.append(rating_key)
        super().remove_from_collection(collection_key, rating_key)


def request_path_engine(conn, collections) -> LifecycleEngine:
    """The engine the web layer builds for Mark and Undo (S1)."""
    return LifecycleEngine(
        conn,
        settings=default_settings(),
        clock=FrozenClock(),
        collections=collections,
        cleanup_backlog_on_cancel=False,
    )


def backlog_engine(conn, collections) -> LifecycleEngine:
    """The engine the scan and the deletion executor build (S3)."""
    return LifecycleEngine(
        conn, settings=default_settings(), clock=FrozenClock(), collections=collections
    )


def leave_a_backlog(engine, conn, keys=("11", "12", "13")) -> int:
    """Cancel three requests whose collection removal failed: the backlog.

    Each item is marked and then undone while the fake port refuses its
    removal, exactly the state a Plex failure leaves: cancelled, still
    recording its member, still owing the collection a removal.
    """
    library = seed_library(conn, "1", "Movies")
    for key in keys:
        item = seed_item(conn, library, key, f"Title {key}")
        engine.collections.fail_remove.add(key)
        engine.undo(engine.mark_for_removal(make_candidate(item, key, f"Title {key}")))
    return library


def pending_cleanups(conn) -> list:
    """The cancelled requests that still record a collection member."""
    return conn.execute(
        "SELECT id, state, collection_added FROM removal_requests"
        " WHERE state = ? AND collection_added = 1 ORDER BY id",
        (RemovalState.CANCELLED.value,),
    ).fetchall()


def test_an_undo_cleans_up_only_its_own_request(conn):
    """Only the cancelled request is touched, whatever the backlog holds (S1)."""
    collections = CountedCollections()
    engine = request_path_engine(conn, collections)
    library = leave_a_backlog(engine, conn)
    assert len(pending_cleanups(conn)) == 3
    item = seed_item(conn, library, "14", "Title 14")
    request = engine.mark_for_removal(make_candidate(item, "14", "Title 14"))
    # Only what the Undo itself does is counted, not the Mark before it.
    collections.removals.clear()
    collections.ensure_calls.clear()

    engine.undo(request)

    # Its own member, once: the three pending requests were not walked.
    assert collections.removals == ["14"]
    assert len(collections.ensure_calls) == 1
    assert len(pending_cleanups(conn)) == 3


def test_the_scan_retry_still_finishes_the_backlog(conn):
    """The pending cleanups are completed later, by the scan's retry (S1)."""
    collections = CountedCollections()
    engine = request_path_engine(conn, collections)
    library = leave_a_backlog(engine, conn)
    item = seed_item(conn, library, "14", "Title 14")
    engine.undo(engine.mark_for_removal(make_candidate(item, "14", "Title 14")))
    assert len(pending_cleanups(conn)) == 3
    collections.removals.clear()

    # The re-check the scan already runs sweeps what a failed write left.
    report = engine.run_recheck()

    assert report.errors == ()
    assert sorted(collections.removals) == ["11", "12", "13"]
    assert pending_cleanups(conn) == []
    assert collections.membership == {"collection-1": set()}


def test_the_scan_engine_still_sweeps_the_backlog_on_a_cancel(conn):
    """The engines of the scan and the executor keep today's sweep (S3)."""
    collections = CountedCollections()
    library = leave_a_backlog(request_path_engine(conn, collections), conn)
    assert len(pending_cleanups(conn)) == 3
    collections.removals.clear()

    engine = backlog_engine(conn, collections)
    item = seed_item(conn, library, "14", "Title 14")
    engine.undo(engine.mark_for_removal(make_candidate(item, "14", "Title 14")))

    # One cancellation finished the three the backlog still owed, as before.
    assert sorted(collections.removals) == ["11", "12", "13", "14"]
    assert pending_cleanups(conn) == []
    assert collections.membership == {"collection-1": set()}
