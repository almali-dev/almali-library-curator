"""Movies not rewatched: S3 category for movies."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import MOVIE_SIZE, full_play, scan, sync_dataset, ts


def test_movies_not_rewatched_matches_with_the_example_reason(conn):
    """The S3 example: watched once by Marc in February 2024, nobody since."""
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [full_play("100", 1, ts(2024, 2, 15))]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Tenet"
    assert candidate.categories == ("movies_not_rewatched",)
    assert candidate.unit.value == "movie"
    assert candidate.reasons == ("Watched once by Marc in February 2024. Nobody since.",)
    assert candidate.size_bytes == MOVIE_SIZE
    assert candidate.rating == 7.3


def test_recent_play_stops_movies_not_rewatched(conn):
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [
        full_play("100", 1, ts(2024, 2, 15)),
        full_play("100", 1, ts(2026, 9, 15)),
    ]
    sync_dataset(conn, data)

    assert scan(conn) == []
