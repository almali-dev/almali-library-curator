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
