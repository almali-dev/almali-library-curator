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


def set_added_at(
    data: FakePlexDataset,
    season_key: str,
    when: int,
    *,
    season_item: bool = True,
    episode_numbers: tuple[int, ...] = (),
) -> None:
    """Date a season and/or some of its episodes, the way Plex reports it."""
    series_key = season_key.rsplit("-", 1)[0]
    if season_item:
        for item in data.children[series_key]:
            if item["ratingKey"] == season_key:
                item["addedAt"] = when
    for number in episode_numbers:
        data.children[season_key][number]["addedAt"] = when


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

    # #135: nothing is stalled. Every season was opened, so the finished
    # seasons of a running series may still propose the older one.
    assert [
        candidate for candidate in scan(conn)
        if "stalled_series" in candidate.categories
    ] == []


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


def test_a_season_added_inside_the_stalled_window_is_not_a_stalled_candidate(conn):
    """#70: there was no time to watch a season added inside the window."""
    from scenarios import sync_dataset

    data = stalled_dataset()
    for season_key in ("200-2", "200-3"):
        set_added_at(data, season_key, ts(2026, 10, 2), episode_numbers=(0, 1))
    sync_dataset(conn, data)

    assert scan(conn) == []


def test_only_the_recently_added_season_leaves_the_stalled_scope(conn):
    """The floor is per season: the old unopened season is still stalled."""
    from scenarios import sync_dataset

    data = stalled_dataset()
    set_added_at(data, "200-2", ts(2026, 10, 2), episode_numbers=(0, 1))
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Lost in Space S3"
    assert candidate.seasons == (3,)
    assert candidate.categories == ("stalled_series",)
    assert candidate.facts["matches"]["stalled_series"]["unopened_seasons"] == [3]
    # The size is only the still-stalled season's episodes.
    assert candidate.size_bytes == 2 * 3 * GB


def test_an_episode_added_inside_the_window_protects_its_season(conn):
    """#70: a season with a newly added episode has not sat unopened."""
    from scenarios import sync_dataset

    data = stalled_dataset()
    # The season's own date is unknown; one of its episodes is new.
    set_added_at(
        data, "200-2", ts(2026, 10, 2), season_item=False, episode_numbers=(0,)
    )
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "Lost in Space S3"
    assert candidate.seasons == (3,)
    assert candidate.size_bytes == 2 * 3 * GB


def test_a_season_added_at_exactly_the_window_is_not_stalled(conn):
    """The floor is strict: at exactly six months the season is still new."""
    from scenarios import sync_dataset

    data = stalled_dataset()
    for season_key in ("200-2", "200-3"):
        set_added_at(data, season_key, ts(2026, 5, 2), episode_numbers=(0, 1))
    sync_dataset(conn, data)

    assert scan(conn) == []


def test_a_season_added_one_day_outside_the_window_still_stalls(conn):
    from scenarios import sync_dataset

    data = stalled_dataset()
    for season_key in ("200-2", "200-3"):
        set_added_at(data, season_key, ts(2026, 5, 1), episode_numbers=(0, 1))
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert [candidate.categories for candidate in candidates] == [("stalled_series",)]
    assert candidates[0].seasons == (2, 3)


def test_an_unopened_season_with_an_old_added_date_still_stalls(conn):
    """An explicitly old season keeps the pre-#70 behaviour."""
    from scenarios import sync_dataset

    data = stalled_dataset()
    for season_key in ("200-2", "200-3"):
        set_added_at(data, season_key, ts(2024, 1, 1), episode_numbers=(0, 1))
    sync_dataset(conn, data)

    candidates = scan(conn)

    assert len(candidates) == 1
    assert candidates[0].title == "Lost in Space S2–S3"
    assert candidates[0].seasons == (2, 3)


def test_an_unopened_season_without_an_added_date_still_stalls(conn):
    """A missing date never hides a season: unknown is not recent."""
    from scenarios import sync_dataset

    # stalled_dataset carries no addedAt on its seasons or episodes.
    sync_dataset(conn, stalled_dataset())

    candidates = scan(conn)

    assert len(candidates) == 1
    assert candidates[0].seasons == (2, 3)
