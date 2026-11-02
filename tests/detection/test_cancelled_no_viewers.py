"""Cancelled, no viewers: S3 category with its degradation rules (S2)."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset

from scenarios import (
    FixedSeriesStatus,
    add_series,
    default_settings,
    full_play,
    scan,
    sync_dataset,
    ts,
    with_thresholds,
)


def cancelled_dataset() -> FakePlexDataset:
    """The S3 example: cancelled in 2022, last play August 2023."""
    data = FakePlexDataset()
    add_series(data, "400", "Westworld", {1: [2, 2]}, addedAt=ts(2022, 1, 10))
    data.history = [
        full_play("400-1-1", 1, ts(2023, 8, 10)),
        full_play("400-1-2", 1, ts(2023, 8, 15)),
    ]
    return data


def test_cancelled_no_viewers_matches_with_the_example_reason(conn):
    sync_dataset(conn, cancelled_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("cancelled", ended_year=2022))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Westworld"
    assert candidate.categories == ("cancelled_no_viewers",)
    assert candidate.unit.value == "series"
    assert candidate.reasons == (
        "Cancelled in 2022. Last play August 2023, nobody mid-season.",
    )


def test_cancelled_no_viewers_degrades_without_an_enricher(conn):
    sync_dataset(conn, cancelled_dataset())

    assert scan(conn) == []


def test_ended_series_with_no_viewers_matches_the_cancelled_category(conn):
    data = FakePlexDataset()
    add_series(data, "410", "Dead to Me", {1: [2, 2]}, addedAt=ts(2022, 1, 10))
    data.history = [
        full_play("410-1-1", 1, ts(2023, 8, 10)),
        full_play("410-1-2", 1, ts(2023, 8, 15)),
    ]
    sync_dataset(conn, data)

    candidates = scan(conn, series_status=FixedSeriesStatus("ended", ended_year=2024))

    assert len(candidates) == 1
    assert candidates[0].reasons == (
        "Cancelled in 2024. Last play August 2023, nobody mid-season.",
    )


def test_continuing_series_is_never_cancelled_no_viewers(conn):
    sync_dataset(conn, cancelled_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    assert scan_for(candidates, "Westworld") == []


def test_mid_show_follower_cancels_the_match(conn):
    """A follower mid-show blocks the category, per the S3 definition."""
    data = FakePlexDataset()
    add_series(data, "420", "Goliath", {1: [2, 2, 2, 2]}, addedAt=ts(2022, 1, 10))
    data.history = [
        full_play("420-1-1", 1, ts(2026, 5, 1)),
        full_play("420-1-2", 1, ts(2026, 5, 1)),
    ]
    sync_dataset(conn, data)

    # Last play is past the 6-month cancelled threshold but inside a
    # 12-month stalled-series window, so Marc is mid-show.
    settings = with_thresholds(default_settings(), stalled_series_months=12)
    candidates = scan(
        conn,
        settings=settings,
        series_status=FixedSeriesStatus("cancelled", ended_year=2024),
    )
    assert candidates == []

    # With the default 6-month window the same history has nobody mid-show.
    candidates = scan(
        conn,
        series_status=FixedSeriesStatus("cancelled", ended_year=2024),
    )
    assert [candidate.categories for candidate in candidates] == [("cancelled_no_viewers",)]


def scan_for(candidates, title: str):
    return [candidate for candidate in candidates if title in candidate.title]
