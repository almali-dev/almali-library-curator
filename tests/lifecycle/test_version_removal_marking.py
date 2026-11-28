"""#133 T5: marking one version of a movie for removal, and its final check.

S1: the owner removes one version while the others stay, through the same
safety as every removal (grace period, recheck, dry run, kill switch). This
slice can mark and recheck a version removal; the executor still refuses it
(``unsupported_scope``) until its own branch exists. Every refusal writes
nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.models import (
    DuplicateMarkError,
    LifecycleError,
    ProtectedItemError,
    UncuratedItemError,
)
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from lifecycle_helpers import (
    FakeCollections, FrozenClock, default_settings, make_candidate,
    seed_item, seed_library, seed_play, seed_user,
)

FOUR_K = "/media/Movies/Heat/Heat 2160p.mkv"
HD = "/media/Movies/Heat/Heat 1080p.mkv"


def seed_version(conn, item, position, media_id, path, resolution="1080"):
    conn.execute(
        "INSERT INTO media_versions (media_item_id, position, plex_media_id, resolution,"
        " size_bytes, file_path) VALUES (?, ?, ?, ?, 10, ?)",
        (item, position, media_id, resolution, path))
    conn.commit()


@pytest.fixture()
def world(conn):
    clock = FrozenClock()
    collections = FakeCollections()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=clock,
                             collections=collections)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Heat")
    seed_version(conn, item, 0, 501, FOUR_K, "4k")
    seed_version(conn, item, 1, 502, HD, "1080")
    return engine, clock, collections, item


def cancel_key(conn):
    """The reason key History stored for the one cancellation."""
    import json

    row = conn.execute("SELECT reason_parts FROM removal_events"
                       " WHERE outcome = 'cancelled'").fetchone()
    return json.loads(row["reason_parts"])[0]["key"]


def counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("removal_requests", "candidates"))


def test_a_version_is_marked_with_its_scope_and_no_collection(conn, world):
    engine, _clock, collections, item = world

    request = engine.mark_version_for_removal(item, 502, HD, marked_by="owner")

    again = engine._request_by_id(request.id)
    assert again.unit is Unit.VERSION
    assert (again.version_media_id, again.version_file_path) == (502, HD)
    assert again.state.value == "marked"
    assert again.due_at - again.marked_at == timedelta(days=engine.settings.grace_days)
    row = conn.execute("SELECT version_media_id, version_file_path FROM removal_requests"
                       " WHERE id = ?", (request.id,)).fetchone()
    assert (row[0], row[1]) == (502, HD)
    # D5: the item itself stays, so it never joins the Leaving soon collection.
    assert (collections.ensure_calls, collections.adds) == ([], 0)
    assert again.collection_members == ()


@pytest.mark.parametrize("media_id,path", [(999, HD), (502, FOUR_K), (502, "/other.mkv")])
def test_a_version_that_is_not_the_stored_one_is_refused(conn, world, media_id, path):
    engine, _clock, _collections, item = world
    before = counts(conn)
    with pytest.raises(LifecycleError):
        engine.mark_version_for_removal(item, media_id, path, marked_by="owner")
    assert counts(conn) == before


def test_the_last_version_is_never_marked(conn, world):
    engine, _clock, _collections, item = world
    conn.execute("DELETE FROM media_versions WHERE plex_media_id = 501")
    conn.commit()
    before = counts(conn)
    with pytest.raises(LifecycleError):
        engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    assert counts(conn) == before


def test_a_series_is_never_marked_as_a_version(conn, world):
    engine, _clock, _collections, _item = world
    library = seed_library(conn, "2", "Shows", section_type="show")
    show = seed_item(conn, library, "21", "Show", item_type="series")
    seed_version(conn, show, 0, 601, "/s/a.mkv")
    seed_version(conn, show, 1, 602, "/s/b.mkv")
    before = counts(conn)
    with pytest.raises(LifecycleError):
        engine.mark_version_for_removal(show, 602, "/s/b.mkv", marked_by="owner")
    assert counts(conn) == before


def test_an_item_absent_from_plex_is_refused(conn, world):
    engine, _clock, _collections, item = world
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?", (item,))
    conn.commit()
    before = counts(conn)
    with pytest.raises(UncuratedItemError):
        engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    assert counts(conn) == before


def test_a_kept_forever_item_is_refused(conn, world):
    from almali_library_curator.protection import ProtectionEngine

    engine, clock, collections, item = world
    protection = ProtectionEngine(conn, settings=engine.settings)
    protection.keep_forever(make_candidate(item, "11", "Heat"))
    engine = LifecycleEngine(conn, settings=engine.settings, clock=clock,
                             collections=collections, protection=protection)
    before = counts(conn)
    with pytest.raises(ProtectedItemError):
        engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    assert counts(conn) == before


def test_a_version_and_the_whole_movie_never_have_open_requests_together(conn, world):
    engine, _clock, _collections, item = world
    engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    before = counts(conn)
    with pytest.raises(DuplicateMarkError):
        engine.mark_for_removal(make_candidate(item, "11", "Heat"), marked_by="owner")
    with pytest.raises(DuplicateMarkError):
        engine.mark_version_for_removal(item, 501, FOUR_K, marked_by="owner")
    assert counts(conn) == before


def test_a_version_is_refused_while_the_whole_movie_is_marked(conn, world):
    engine, _clock, _collections, item = world
    engine.mark_for_removal(make_candidate(item, "11", "Heat"), marked_by="owner")
    before = counts(conn)
    with pytest.raises(DuplicateMarkError):
        engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    assert counts(conn) == before


def test_the_final_check_passes_a_version_still_duplicated(conn, world):
    engine, clock, _collections, item = world
    request = engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    clock.advance(days=15)

    report = engine.run_recheck()

    assert [o.request.id for o in report.passed] == [request.id]


def test_a_play_after_marking_cancels_the_version_removal(conn, world):
    engine, clock, _collections, item = world
    request = engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    user = seed_user(conn, "7", "Ana")
    seed_play(conn, user, "11", clock() + timedelta(days=2))
    clock.advance(days=15)

    report = engine.run_recheck()

    assert report.passed == ()
    row = engine._request_by_id(request.id)
    assert row.state.value == "cancelled"
    assert cancel_key(conn) == "removal.cancel.started_watching"


@pytest.mark.parametrize("change", [
    "DELETE FROM media_versions WHERE plex_media_id = 502",
    "DELETE FROM media_versions WHERE plex_media_id = 501",
    "UPDATE media_versions SET file_path = '/elsewhere.mkv' WHERE plex_media_id = 502",
])
def test_the_final_check_cancels_when_the_version_is_no_longer_a_duplicate(conn, world, change):
    engine, clock, _collections, item = world
    request = engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    conn.execute(change)
    conn.commit()
    clock.advance(days=15)

    report = engine.run_recheck()

    assert report.passed == ()
    row = engine._request_by_id(request.id)
    assert row.state.value == "cancelled"
    assert cancel_key(conn) == "removal.cancel.no_longer_matches"


def test_the_executor_refuses_a_version_until_it_has_a_branch_for_it(conn, world):
    engine, clock, _collections, item = world
    request = engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    clock.advance(days=15)
    passed = [o.request.id for o in engine.run_recheck().passed]
    assert passed == [request.id]

    report = DeletionExecutor(engine, plex=None, radarr=None, sonarr=None).run_deletions(
        passed_ids=passed)

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.unsupported_scope")]
    assert engine._request_by_id(request.id).deleted_at is None


def test_the_final_check_never_adds_a_version_removal_to_the_collection(conn, world):
    # D5: the item stays, so the collection retry must not add it either
    # (verifier finding on the T1 slice: the retry used the item's own key
    # for any unit but seasons).
    engine, clock, collections, item = world
    engine.mark_version_for_removal(item, 502, HD, marked_by="owner")
    clock.advance(days=1)

    engine.run_recheck()

    assert (collections.ensure_calls, collections.adds) == ([], 0)
    assert collections.membership == {}
