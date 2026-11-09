"""The single-item scan (#5, #6): the full scan restricted to some items.

The final re-check and the Mark for removal action re-derive one item's
candidates. They must get exactly what the full scan says about that
item, through the same users, thresholds, enrichers, library scope and
protection, without paying for a scan of the whole library.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, movie

from scenarios import (
    NOW,
    FixedSeriesStatus,
    add_series,
    default_settings,
    full_play,
    partial_play,
    sync_dataset,
    ts,
)

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.enrichers import Enrichers
from almali_library_curator.protection import ProtectionEngine

GUEST_ID = 3


def mixed_dataset() -> FakePlexDataset:
    """Every category, a protected movie, an excluded user and a shared
    history, so one item's facts cannot leak into another's."""
    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
        {"id": GUEST_ID, "name": "Guest"},
    ]
    data.items["1"] = [
        movie("100", "Oldboy", addedAt=ts(2024, 4, 1)),
        movie("101", "Tenet", addedAt=ts(2023, 1, 1), rating=7.3),
        movie("102", "Heat", addedAt=ts(2023, 1, 1), rating=9.1),
        movie("103", "Alien", addedAt=ts(2026, 10, 1)),
    ]
    add_series(data, "500", "Foundation", {1: [1, 1], 2: [2, 2]}, addedAt=ts(2024, 1, 5))
    add_series(data, "600", "Deadwood", {1: [1]}, addedAt=ts(2024, 4, 1))
    add_series(data, "700", "Carnivàle", {1: [1], 2: [2], 3: [3]}, addedAt=ts(2024, 4, 1))
    data.history = [
        full_play("100", GUEST_ID, ts(2024, 6, 1)),
        full_play("101", 1, ts(2024, 2, 15)),
        full_play("101", 2, ts(2024, 3, 15)),
        full_play("102", 1, ts(2024, 2, 15)),
        full_play("500-1-1", 1, ts(2025, 1, 10)),
        full_play("500-1-2", 1, ts(2025, 1, 20)),
        full_play("500-1-1", 2, ts(2025, 1, 12)),
        full_play("500-1-2", 2, ts(2025, 1, 18)),
        partial_play("500-2-1", 2, ts(2025, 2, 10), 0.4),
        full_play("700-1-1", 2, ts(2025, 3, 1)),
        full_play("700-1-1", GUEST_ID, ts(2026, 10, 1)),
    ]
    return data


def engine(conn, **overrides) -> DetectionEngine:
    settings = default_settings(excluded_users=("Guest",), **overrides)
    return DetectionEngine(
        conn,
        enrichers=Enrichers(
            series_status=FixedSeriesStatus("cancelled", ended_year=2007)
        ),
        clock=lambda: NOW,
        settings=settings,
        protector=ProtectionEngine(conn, settings=settings),
    )


def item_ids(conn) -> list[int]:
    return [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM media_items WHERE item_type IN ('movie', 'series') ORDER BY id"
        )
    ]


def test_each_item_scan_equals_the_full_scan_restricted_to_it(conn):
    sync_dataset(conn, mixed_dataset())
    full = engine(conn).scan_with_protection()
    # The fixture must exercise both outcomes, or the equality proves little.
    assert full.candidates and full.protected

    for identifier in item_ids(conn):
        single = engine(conn).scan_items([identifier])

        assert single.candidates == tuple(
            c for c in full.candidates if c.media_item_id == identifier
        )
        assert single.protected == tuple(
            p for p in full.protected if p.candidate.media_item_id == identifier
        )


def test_a_scan_of_several_items_keeps_the_full_scan_order(conn):
    sync_dataset(conn, mixed_dataset())
    full = engine(conn).scan_with_protection()
    wanted = item_ids(conn)[::2]

    several = engine(conn).scan_items(wanted)

    assert several.candidates == tuple(
        c for c in full.candidates if c.media_item_id in wanted
    )


def test_an_item_outside_the_curated_scope_yields_nothing(conn):
    sync_dataset(conn, mixed_dataset())
    movie_id = conn.execute(
        "SELECT id FROM media_items WHERE plex_rating_key = '100'"
    ).fetchone()["id"]
    assert engine(conn).scan_items([movie_id]).candidates

    restricted = engine(conn, curated_libraries=("2",)).scan_items([movie_id])

    assert restricted.candidates == () and restricted.protected == ()


@pytest.mark.parametrize("identifiers", [[], [999_999]])
def test_no_item_or_an_unknown_item_yields_nothing(conn, identifiers):
    sync_dataset(conn, mixed_dataset())

    outcome = engine(conn).scan_items(identifiers)

    assert outcome.candidates == () and outcome.protected == ()


def test_the_full_scan_still_reports_every_category(conn):
    """#4 regroups the events: the full scan's answer must not move."""
    sync_dataset(conn, mixed_dataset())

    candidates = engine(conn).scan()

    # Captured from the scan before #4 regrouped it.
    assert [(c.title, c.categories) for c in candidates] == [
        ("Oldboy", ("never_watched",)),
        ("Tenet", ("movies_not_rewatched",)),
        ("Carnivàle", ("cancelled_no_viewers",)),
        ("Foundation", ("cancelled_no_viewers",)),
        ("Carnivàle S2–S3", ("stalled_series",)),
        ("Foundation S1", ("seasons_everyone_finished",)),
        ("Carnivàle S1", ("seasons_everyone_finished",)),
        ("Deadwood", ("never_watched", "cancelled_no_viewers")),
    ]
    assert [p.candidate.title for p in engine(conn).scan_with_protection().protected] == [
        "Heat"
    ]
