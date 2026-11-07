"""Seasons everyone finished: S3 category and the follower/watched rules."""

from __future__ import annotations

from datetime import datetime, timezone

from fake_plex_server import FakePlexDataset

from scenarios import add_series, full_play, partial_play, scan, sync_dataset, ts


def everyone_finished_dataset() -> FakePlexDataset:
    """The S3 example: Marc and Anna both finished S1, S2 is in progress."""
    data = FakePlexDataset()
    add_series(data, "500", "Foundation", {1: [1, 1], 2: [2, 2]}, addedAt=ts(2024, 1, 5))
    data.history = [
        full_play("500-1-1", 1, ts(2025, 1, 10)),
        full_play("500-1-2", 1, ts(2025, 1, 20)),
        full_play("500-1-1", 2, ts(2025, 1, 12)),
        full_play("500-1-2", 2, ts(2025, 1, 18)),
        partial_play("500-2-1", 2, ts(2025, 2, 10), 0.4),
    ]
    return data


def test_everyone_finished_matches_with_the_example_reason(conn):
    sync_dataset(conn, everyone_finished_dataset())

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Foundation S1"
    assert candidate.categories == ("seasons_everyone_finished",)
    assert candidate.unit.value == "seasons"
    assert candidate.reasons == (
        "Marc and Anna (the only followers) both finished S1. S2 is in progress.",
    )
    # The unit applies to the finished seasons only.
    assert candidate.seasons == (1,)
    assert candidate.size_bytes == 2 * 1 * 1024**3


def test_users_who_never_started_do_not_count_towards_everyone(conn):
    data = everyone_finished_dataset()
    data.accounts = data.accounts + [{"id": 3, "name": "Joe"}]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    assert candidates[0].reasons == (
        "Marc and Anna (the only followers) both finished S1. S2 is in progress.",
    )


def test_season_one_follower_did_not_finish_is_not_everyone_finished(conn):
    data = FakePlexDataset()
    add_series(data, "510", "Severance", {1: [1, 1], 2: [2, 2]}, addedAt=ts(2024, 1, 5))
    data.history = [
        full_play("510-1-1", 1, ts(2025, 1, 10)),
        full_play("510-1-2", 1, ts(2025, 1, 20)),
        full_play("510-1-1", 2, ts(2025, 1, 12)),
        partial_play("510-2-1", 2, ts(2025, 2, 10), 0.4),
    ]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert [candidate for candidate in candidates if "Severance" in candidate.title] == []


def test_finished_last_month_is_within_the_everyone_finished_threshold(conn):
    """All followers finished S1 exactly one month of plays ago: not yet."""
    data = FakePlexDataset()
    add_series(data, "520", "Silo", {1: [1], 2: [2]}, addedAt=ts(2024, 1, 5))
    data.history = [
        full_play("520-1-1", 1, ts(2026, 10, 2)),
        partial_play("520-2-1", 2, ts(2025, 2, 10), 0.4),
    ]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert [candidate for candidate in candidates if "Silo" in candidate.title] == []
