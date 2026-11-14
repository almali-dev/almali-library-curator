"""The sync reads a show section in bulk instead of one call per series (#3).

A full sync used to ask Plex for the children of every series and of every
season: about 2,320 requests for 300 series, and 457 s of sleep in the rate
limiter. The section reads answer the same rows in a handful of requests, so
the sync spends its time on the data instead of on the throttle.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    episode,
    history_entry,
    movie,
    season,
    series,
)
from sync_snapshot import snapshot

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


@pytest.fixture()
def dataset() -> FakePlexDataset:
    """A show section with five series, three seasons each, two episodes each."""
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet"), movie("101", "The Gray Man")]
    shows = []
    for show_number in range(5):
        series_key = f"2{show_number}"
        shows.append(series(series_key, f"Show {show_number}"))
        seasons = []
        for season_number in range(1, 4):
            season_key = f"{series_key}{season_number}"
            seasons.append(season(season_key, series_key, season_number))
            data.children[season_key] = [
                episode(f"{season_key}0{episode_number}", series_key, season_key, episode_number)
                for episode_number in range(1, 3)
            ]
        data.children[series_key] = seasons
    data.items["2"] = shows
    data.history = [history_entry("100", 1, 1_700_000_000, 6_000_000, 6_000_000)]
    return data


@pytest.fixture()
def server(dataset):
    with FakePlexServer(dataset) as running:
        yield running


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "almali-library-curator.db")
    yield connection
    connection.close()


def build_client(server) -> PlexClient:
    return PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=100,
        rate_limit_per_second=1000.0,
        backoff_seconds=0.0,
    )


def test_a_show_section_is_read_with_a_handful_of_requests(server, conn):
    summary = PlexSync(build_client(server), conn).run()

    # The old sync asked Plex once per series and once per season: 25
    # requests for this library. The section reads answer the same rows.
    assert len(server.requests) <= 10
    assert not any(
        record["path"].startswith("/library/metadata/") for record in server.requests
    )
    assert (summary.media_items, summary.episodes) == (22, 30)


def test_the_seasons_and_the_episodes_are_read_from_the_section(server, conn):
    PlexSync(build_client(server), conn).run()

    bulk = [
        record for record in server.requests if record["query"].get("type") in ("3", "4")
    ]

    assert bulk, "the seasons and the episodes were not read in bulk"
    assert {record["query"]["type"] for record in bulk} == {"3", "4"}
    assert {record["path"] for record in bulk} == {"/library/sections/2/all"}


def test_a_server_that_refuses_the_bulk_read_stores_the_same_rows(
    dataset, conn, tmp_path
):
    refusing_dataset = deepcopy(dataset)
    refusing_dataset.always_fail["/library/sections/2/all?type=3"] = 404
    refusing_dataset.always_fail["/library/sections/2/all?type=4"] = 404

    with FakePlexServer(refusing_dataset) as refusing:
        PlexSync(build_client(refusing), conn).run()
    fallback_rows = snapshot(conn)

    other = init_db(tmp_path / "other.db")
    with FakePlexServer(deepcopy(dataset)) as working:
        PlexSync(build_client(working), other).run()

    assert fallback_rows == snapshot(other)


def test_a_server_that_answers_the_bulk_read_with_nothing_stores_the_same_rows(
    dataset, conn, tmp_path
):
    # The seasons and the episodes are what the detection engine reads, so
    # an empty answer is never taken for the truth: the rows are read the
    # old way when the series report children the bulk read did not answer.
    silent_dataset = deepcopy(dataset)
    silent_dataset.bulk_reads_answer_nothing = True

    with FakePlexServer(silent_dataset) as silent:
        PlexSync(build_client(silent), conn).run()
    fallback_rows = snapshot(conn)

    other = init_db(tmp_path / "other.db")
    with FakePlexServer(deepcopy(dataset)) as working:
        PlexSync(build_client(working), other).run()

    assert fallback_rows == snapshot(other)
