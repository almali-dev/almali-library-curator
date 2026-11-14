"""Movies not rewatched: S3 category for movies."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import MOVIE_SIZE, full_play, partial_play, scan, sync_dataset, ts


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


def test_movies_not_rewatched_matches_a_plex_history_entry_without_a_position(conn):
    """#70: a Plex history entry is evidence of a watch on its own."""
    from scenarios import history_play

    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [history_play("100", 1, ts(2024, 2, 15))]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.categories == ("movies_not_rewatched",)
    assert candidate.reasons == ("Watched once by Marc in February 2024. Nobody since.",)
    assert candidate.facts["matches"]["movies_not_rewatched"] == {
        "watch_count": 1,
        "watchers": ["Marc"],
        "last_played_at": "2024-02-15T12:00:00+00:00",
    }


def test_partial_plex_history_still_does_not_count_as_watched(conn):
    """When Plex reports the position, the 90% rule still decides."""
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", addedAt=ts(2020, 12, 1))]
    data.history = [partial_play("100", 1, ts(2024, 2, 15), 0.4)]
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert candidates == []
