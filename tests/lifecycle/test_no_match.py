"""S5 step 4, exit 1 — the item no longer matches its category.

The rematcher is the real production adapter (:class:`DetectionRematcher`)
over a database synced from the fake Plex server; the play history then
changes, the database re-syncs, and the re-check cancels.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie

from lifecycle_helpers import FakeCollections, FrozenClock, full_play, ts, default_settings

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import (
    DetectionRematcher,
    LifecycleEngine,
    RemovalState,
)
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.protection import ProtectionEngine

# Marked on 20 October 2026; grace 3 days; re-checked after the due date.
MARKED_AT = ts(2026, 10, 20)


@pytest.fixture()
def clock():
    return FrozenClock()


@pytest.fixture()
def dataset():
    data = FakePlexDataset()
    data.items["1"] = [movie("11", "Tenet", rating=7.3)]
    data.history = [full_play("11", 1, ts(2024, 2, 15))]
    return data


@pytest.fixture()
def synced(conn, dataset):
    with FakePlexServer(dataset) as server:
        client = PlexClient(
            base_url=server.base_url,
            token="test-token",
            page_size=100,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        PlexSync(client, conn).run()
        yield client


def test_a_category_that_no_longer_matches_cancels(conn, clock, dataset, synced):
    settings = default_settings()
    detection = DetectionEngine(conn, clock=clock, settings=settings)
    candidate = next(c for c in detection.scan() if c.title == "Tenet")
    engine = LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=FakeCollections(),
        protection=ProtectionEngine(conn, settings=settings),
        rematcher=DetectionRematcher(detection),
    )
    engine.mark_for_removal(candidate, grace_days=3)

    # The owner watches the movie during the grace period.
    dataset.history.append(full_play("11", 1, ts(2026, 10, 25)))
    PlexSync(synced, conn).run()

    clock.advance(days=11)
    report = engine.run_recheck()

    assert len(report.cancelled) == 1
    cancelled = report.cancelled[0]
    assert cancelled.exit_code.value == "no_longer_matches"
    assert cancelled.cancel_reason == translate(
        "removal.cancel.no_longer_matches", "en"
    )
    row = conn.execute("SELECT state, recheck_status FROM removal_requests").fetchone()
    assert row["state"] == RemovalState.CANCELLED.value
    assert row["recheck_status"] == "no_longer_matches"


def _forbid_full_scans(detection):
    """#5: the re-check must never pay for a scan of the whole library."""

    def full_scan(*_args, **_kwargs):
        raise AssertionError("the re-check ran a full detection scan")

    detection.scan = full_scan
    detection.scan_with_protection = full_scan


@pytest.mark.parametrize("watched_again, expected", [(True, "cancelled"), (False, "passed")])
def test_the_recheck_rematches_without_a_full_scan(
    conn, clock, dataset, synced, watched_again, expected
):
    settings = default_settings()
    detection = DetectionEngine(conn, clock=clock, settings=settings)
    candidate = next(c for c in detection.scan() if c.title == "Tenet")
    engine = LifecycleEngine(
        conn,
        settings=settings,
        clock=clock,
        collections=FakeCollections(),
        protection=ProtectionEngine(conn, settings=settings),
        rematcher=DetectionRematcher(detection),
    )
    engine.mark_for_removal(candidate, grace_days=3)
    if watched_again:
        # A rewatch long after the mark moves the movie out of its category.
        dataset.history.append(full_play("11", 1, ts(2026, 10, 19)))
        PlexSync(synced, conn).run()
    _forbid_full_scans(detection)

    clock.advance(days=11)
    report = engine.run_recheck()

    assert report.errors == ()
    if expected == "cancelled":
        assert [o.exit_code.value for o in report.cancelled] == ["no_longer_matches"]
        assert report.passed == ()
    else:
        assert len(report.passed) == 1 and report.cancelled == ()


def test_the_rematcher_answers_only_for_the_marked_scope(conn, clock, dataset, synced):
    """A second candidate in the library never answers for the marked one."""
    settings = default_settings()
    detection = DetectionEngine(conn, clock=clock, settings=settings)
    tenet = next(c for c in detection.scan() if c.title == "Tenet")
    rematcher = DetectionRematcher(detection)
    _forbid_full_scans(detection)

    assert rematcher.still_matches(tenet)
    other_item = tenet.__class__(**{**tenet.__dict__, "media_item_id": tenet.media_item_id + 1000})
    assert not rematcher.still_matches(other_item)
