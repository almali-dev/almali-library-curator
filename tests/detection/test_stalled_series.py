"""Stalled series: S3 category and its example scenario."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset

from scenarios import GB, MOVIE_SIZE, add_series, full_play, partial_play, scan, ts


def stalled_dataset() -> FakePlexDataset:
    """The S3 example: Anna watched S1 in March 2025, S2 and S3 never opened."""
    data = FakePlexDataset()
    add_series(
        data,
        "200",
        "Lost in Space",
        {1: [1, 1], 2: [2, 2], 3: [3, 3]},
        addedAt=ts(2023, 6, 1),
    )
    data.history = [
        partial_play("200-1-1", 1, ts(2024, 12, 1), 0.5),
        full_play("200-1-1", 2, ts(2025, 3, 15)),
    ]
    return data


def test_stalled_series_matches_with_the_example_reason(conn):
    from scenarios import sync_dataset

    sync_dataset(conn, stalled_dataset())

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Lost in Space S2–S3"
    assert candidate.categories == ("stalled_series",)
    assert candidate.unit.value == "seasons"
    assert candidate.reasons == ("Anna watched S1 in March 2025. S2 and S3 were never opened.",)
    # The size is the sum of the unopened seasons' episode sizes only.
    assert candidate.size_bytes == 2 * 2 * GB + 2 * 3 * GB


def test_stalled_series_at_exactly_the_threshold_does_not_match(conn):
    from scenarios import sync_dataset

    data = stalled_dataset()
    data.history = [full_play("200-1-1", 2, ts(2026, 5, 2))]
    sync_dataset(conn, data)

    assert scan(conn) == []


def test_stalled_series_one_day_past_the_threshold_matches(conn):
    from scenarios import sync_dataset

    data = stalled_dataset()
    data.history = [full_play("200-1-1", 2, ts(2026, 5, 1))]
    sync_dataset(conn, data)

    candidates = scan(conn)
    assert [candidate.categories for candidate in candidates] == [("stalled_series",)]


def test_series_with_every_season_opened_is_not_stalled(conn):
    from scenarios import sync_dataset

    data = FakePlexDataset()
    add_series(data, "200", "Lost in Space", {1: [1], 2: [2]}, addedAt=ts(2023, 6, 1))
    data.history = [
        full_play("200-1-1", 2, ts(2025, 3, 15)),
        full_play("200-2-1", 2, ts(2025, 3, 15)),
    ]
    sync_dataset(conn, data)

    assert scan(conn) == []


def test_movie_size_comes_from_its_own_file(conn):
    """The movie size rule of S3, through the same public interface."""
    from fake_plex_server import movie

    from scenarios import sync_dataset

    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2024, 4, 1))]
    data.history = []
    sync_dataset(conn, data)

    candidates = scan(conn)
    assert len(candidates) == 1
    assert candidates[0].size_bytes == MOVIE_SIZE
