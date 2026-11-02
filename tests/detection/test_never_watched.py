"""Never watched: S3 category for series and movies."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import add_series, scan, ts


def never_watched_dataset() -> FakePlexDataset:
    """The S3 example: added April 2024, 0 episodes played by any of 3 users."""
    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
        {"id": 3, "name": "Joe"},
    ]
    add_series(data, "300", "The Leftovers", {1: [1, 1], 2: [2, 2]}, addedAt=ts(2024, 4, 1))
    return data


def test_never_watched_series_matches_with_the_example_reason(conn):
    from scenarios import sync_dataset

    sync_dataset(conn, never_watched_dataset())

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "The Leftovers"
    assert candidate.categories == ("never_watched",)
    assert candidate.unit.value == "series"
    assert candidate.reasons == (
        "Added April 2024. 0 episodes played by any of the 3 users.",
    )
    # Series unit: the sum over the seasons the category names (all of them).
    assert candidate.size_bytes == 2 * 1 * 1024**3 + 2 * 2 * 1024**3


def test_never_watched_movie_uses_the_movie_reason(conn):
    from scenarios import sync_dataset

    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
        {"id": 3, "name": "Joe"},
    ]
    data.items["1"] = [movie("100", "The Gray Man", addedAt=ts(2024, 4, 1))]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    assert candidates[0].unit.value == "movie"
    assert candidates[0].reasons == (
        "Added April 2024. 0 plays by any of the 3 users.",
    )


def test_any_play_by_any_user_stops_never_watched(conn):
    """A 40% play is still a play: not never watched, but stalled instead."""
    from scenarios import partial_play, sync_dataset

    data = never_watched_dataset()
    data.accounts = [{"id": 1, "name": "Marc"}, {"id": 2, "name": "Anna"}]
    data.history = [partial_play("300-1-1", 2, ts(2025, 1, 10), 0.4)]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert [candidate.categories for candidate in candidates] == [("stalled_series",)]


def test_never_watched_at_exactly_the_threshold_does_not_match(conn):
    from scenarios import sync_dataset

    data = never_watched_dataset()
    data.items["2"] = []
    data.items["1"] = [movie("100", "The Gray Man", addedAt=ts(2026, 8, 2))]
    sync_dataset(conn, data)

    assert scan(conn) == []


def test_never_watched_one_day_past_the_threshold_matches(conn):
    from scenarios import sync_dataset

    data = never_watched_dataset()
    data.items["2"] = []
    data.items["1"] = [movie("100", "The Gray Man", addedAt=ts(2026, 8, 1))]
    sync_dataset(conn, data)

    candidates = scan(conn)
    assert [candidate.categories for candidate in candidates] == [("never_watched",)]
