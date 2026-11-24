"""Never watched: S3 category for series and movies."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import (
    add_series,
    candidates_for,
    default_settings,
    history_play,
    scan,
    ts,
)


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
    # #122 S2: none of the 3 accounts has a recorded play, so the sentence
    # carries no user count and the item still appears.
    assert candidate.reasons == (
        "Added April 2024. No episode has ever been played.",
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
        "Added April 2024. It has never been played.",
    )
    assert candidates[0].facts["matches"]["never_watched"]["user_count"] == 0


def viewers_dataset() -> FakePlexDataset:
    """#122: a server that knows 356 accounts, of which 3 ever played."""
    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
        {"id": 3, "name": "Joe"},
    ] + [{"id": index, "name": f"Friend {index}"} for index in range(4, 357)]
    data.items["1"] = [
        movie("100", "The Gray Man", addedAt=ts(2024, 4, 1)),
        movie("200", "Heat", addedAt=ts(2024, 4, 1)),
    ]
    data.history = [
        history_play("200", 1, ts(2024, 5, 1)),
        history_play("200", 2, ts(2024, 5, 2)),
        history_play("200", 3, ts(2024, 5, 3)),
    ]
    return data


def test_never_watched_counts_only_users_with_a_recorded_play(conn):
    """#122 S1: accounts that never played anything are not counted."""
    from scenarios import sync_dataset

    sync_dataset(conn, viewers_dataset())

    (candidate,) = candidates_for(scan(conn), "The Gray Man")

    assert candidate.reasons == ("Added April 2024. 0 plays by any of the 3 users.",)
    assert candidate.facts["matches"]["never_watched"]["user_count"] == 3


def test_never_watched_count_skips_excluded_users_with_plays(conn):
    """#122 S1: an excluded user's plays never make them counted."""
    from scenarios import sync_dataset

    sync_dataset(conn, viewers_dataset())
    conn.execute("UPDATE users SET excluded = 1 WHERE title = 'Anna'")
    conn.commit()

    (candidate,) = candidates_for(
        scan(conn, settings=default_settings(excluded_users=("joe",))), "The Gray Man"
    )

    assert candidate.reasons == ("Added April 2024. 0 plays by any of the 1 users.",)
    assert candidate.facts["matches"]["never_watched"]["user_count"] == 1


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
