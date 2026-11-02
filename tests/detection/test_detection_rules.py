"""Cross-category rules: one appearance per item, exclusions, watched."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import (
    FixedRating,
    FixedSeriesStatus,
    add_series,
    default_settings,
    full_play,
    insert_event,
    partial_play,
    scan,
    sync_dataset,
    ts,
)
from test_everyone_finished import everyone_finished_dataset

GUEST_ID = 3


def guest_dataset() -> FakePlexDataset:
    """Marc, Anna and an excluded Guest whose plays must count for nothing."""
    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
        {"id": GUEST_ID, "name": "Guest"},
    ]
    data.items["1"] = [movie("100", "Oldboy", addedAt=ts(2024, 4, 1))]
    add_series(data, "700", "Carnivàle", {1: [1], 2: [2]}, addedAt=ts(2024, 4, 1))
    data.history = [
        full_play("100", GUEST_ID, ts(2024, 6, 1)),
        full_play("700-1-1", GUEST_ID, ts(2024, 6, 1)),
    ]
    return data


def excluded_settings() -> object:
    return default_settings(excluded_users=("Guest",))


def test_item_matching_two_categories_appears_once_with_two_reasons(conn):
    data = FakePlexDataset()
    add_series(data, "600", "Deadwood", {1: [1]}, addedAt=ts(2024, 4, 1))
    sync_dataset(conn, data)

    candidates = scan(
        conn, series_status=FixedSeriesStatus("cancelled", ended_year=2007)
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.categories == ("never_watched", "cancelled_no_viewers")
    assert candidate.reasons == (
        "Added April 2024. 0 episodes played by any of the 2 users.",
        "Cancelled in 2007. Never played, nobody mid-season.",
    )
    # The merged facts carry both categories' evidence.
    assert set(candidate.facts["matches"]) == {"never_watched", "cancelled_no_viewers"}


def test_excluded_user_plays_do_not_make_a_movie_watched(conn):
    sync_dataset(conn, guest_dataset())

    candidates = scan(conn, settings=excluded_settings())

    assert [candidate.title for candidate in candidates] == ["Oldboy", "Carnivàle"]
    movie_candidate = candidates[0]
    assert movie_candidate.categories == ("never_watched",)
    assert movie_candidate.reasons == (
        "Added April 2024. 0 plays by any of the 2 users.",
    )


def test_excluded_user_does_not_count_as_a_follower(conn):
    sync_dataset(conn, guest_dataset())

    candidates = scan(conn, settings=excluded_settings())

    series_candidate = candidates[1]
    # The guest opened S1, but with the guest excluded nobody watched part
    # of the show: it is never watched, not stalled.
    assert series_candidate.categories == ("never_watched",)
    assert series_candidate.reasons == (
        "Added April 2024. 0 episodes played by any of the 2 users.",
    )


def test_episode_played_past_90_percent_counts_as_watched(conn):
    """S3: played past 90% in history counts as watched, flag or no flag."""
    from datetime import datetime, timezone

    sync_dataset(conn, everyone_finished_dataset())
    # Marc's second S1 episode only exists as a 95%-positioned event whose
    # watched flag is unset in the synced database.
    conn.execute("DELETE FROM play_events WHERE user_id = 1 AND plex_rating_key = '500-1-2'")
    conn.commit()
    insert_event(
        conn,
        "Marc",
        "500-1-2",
        datetime(2025, 1, 20, 12, 0, tzinfo=timezone.utc),
        position_ms=2_565_000,
    )

    candidates = scan(conn)

    assert len(candidates) == 1
    assert candidates[0].reasons == (
        "Marc and Anna (the only followers) both finished S1. S2 is in progress.",
    )


def test_episode_played_to_40_percent_is_not_watched(conn):
    from datetime import datetime, timezone

    sync_dataset(conn, everyone_finished_dataset())
    conn.execute("DELETE FROM play_events WHERE user_id = 1 AND plex_rating_key = '500-1-2'")
    conn.commit()
    insert_event(
        conn,
        "Marc",
        "500-1-2",
        datetime(2025, 1, 20, 12, 0, tzinfo=timezone.utc),
        position_ms=1_080_000,
    )

    candidates = scan(conn)

    assert [candidate for candidate in candidates if "Foundation" in candidate.title] == []


def test_rating_falls_back_to_enrichers_when_plex_has_none(conn):
    data = FakePlexDataset()
    add_series(data, "800", "The OA", {1: [1]}, rating=None, addedAt=ts(2026, 1, 1))
    data.items["1"] = [movie("101", "Sunset", rating=None, addedAt=ts(2026, 1, 1))]
    sync_dataset(conn, data)

    candidates = scan(
        conn,
        series_status=FixedSeriesStatus("cancelled", ended_year=2019, rating=7.1),
        rating=FixedRating(6.4),
    )

    by_title = {candidate.title: candidate for candidate in candidates}
    assert by_title["The OA"].rating == 7.1
    assert by_title["Sunset"].rating == 6.4
