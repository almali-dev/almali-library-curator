"""One end-to-end run of the lifecycle (S5 steps 1 to 4).

Fake Plex server, ``PlexSync``, the real detection engine, the real
protection engine, the real production rematcher and the PlexClient
itself as the collections port — from a scan's candidate, through a
mark, past the grace period, to a passing re-check that deletes
nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie

from lifecycle_helpers import FrozenClock, default_settings, full_play, ts

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.lifecycle import (
    DetectionRematcher,
    LifecycleEngine,
    RemovalState,
)
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.protection import ProtectionEngine


@pytest.fixture()
def clock():
    return FrozenClock()


@pytest.fixture()
def dataset():
    data = FakePlexDataset()
    data.items["1"] = [movie("11", "Tenet", rating=7.3)]
    data.history = [full_play("11", 1, ts(2024, 2, 15))]
    return data


def test_mark_then_recheck_passes_through_without_deleting(conn, clock, dataset):
    settings = default_settings()
    with FakePlexServer(dataset) as server:
        client = PlexClient(
            base_url=server.base_url,
            token="test-token",
            page_size=100,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        PlexSync(client, conn).run()

        detection = DetectionEngine(conn, clock=clock, settings=settings)
        candidate = next(c for c in detection.scan() if c.title == "Tenet")
        engine = LifecycleEngine(
            conn,
            settings=settings,
            clock=clock,
            collections=client,
            protection=ProtectionEngine(conn, settings=settings),
            rematcher=DetectionRematcher(detection),
        )

        request = engine.mark_for_removal(candidate, grace_days=3)

        # The item is in the real (fake-server) Leaving soon collection.
        collection = next(
            entry
            for entry in dataset.collections["1"]
            if entry["title"] == "Leaving soon"
        )
        assert dataset.collection_items[collection["ratingKey"]] == {"11"}
        assert request.collection_added is True

        clock.advance(days=4)
        report = engine.run_recheck()

        assert len(report.passed) == 1
        reloaded = engine.open_requests()[0]
        assert reloaded.state is RemovalState.RECHECK_PASSED
        assert reloaded.recheck_status == "passed"
        # The item STAYS in the collection until the deletion (T7).
        assert dataset.collection_items[collection["ratingKey"]] == {"11"}
        # Nothing was deleted: no removal event, and the collection holds it.
        assert conn.execute("SELECT COUNT(*) FROM removal_events").fetchone()[0] == 0

        # The collection was created exactly once over the wire.
        creates = [
            r
            for r in server.requests_for("/library/sections/1/collections")
            if r["method"] == "POST"
        ]
        assert len(creates) == 1
        assert creates[0]["body"] == {"title": "Leaving soon"}
