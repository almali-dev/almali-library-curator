"""The rows the sync stores do not change with the v0.28.0 optimisations.

Issue #3 reads the seasons and the episodes of a section in bulk, and issue
#10 writes the rows in batches. Both change how the sync talks to Plex and
to SQLite, never what it stores. This snapshot pins every row of every
synced table, so a change in the stored data fails here instead of reaching
the detection engine and the deletion pipeline.
"""

from __future__ import annotations

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
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet"), movie("101", "The Gray Man")]
    data.items["2"] = [series("200", "Lost in Space")]
    data.children["200"] = [season("210", "200", 1), season("211", "200", 2)]
    data.children["210"] = [
        episode("300", "200", "210", 1),
        episode("301", "200", "210", 2),
    ]
    data.history = [
        history_entry("100", 1, 1_700_000_000, 6_000_000, 6_000_000),
        history_entry("200", 2, 1_600_000_000, 60_000, 2_700_000),
    ]
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


# libraries: id, plex_key, title, section_type, protected
# media_items: id, library_id, plex_rating_key, parent_rating_key, title,
#   item_type, season_number, added_at, last_played_at, rating, rating_source,
#   production_status, file_path, file_size_bytes, disk, hardlink_count,
#   sonarr_id, radarr_id, monitored, tmdb_id, tvdb_id, imdb_id, year, plex_present,
#   thumb, imdb_rating, rt_critic
# episodes: id, media_item_id, plex_rating_key, series_rating_key,
#   season_number, episode_number, title, file_path, file_size_bytes, added_at,
#   disk, hardlink_count, plex_present
# users: id, plex_user_id, title, is_owner, excluded
# play_events: id, user_id, plex_rating_key, watched, played_at, position_ms,
#   duration_ms, source
EXPECTED = {
    "libraries": [
        (1, "1", "Movies", "movie", 0),
        (2, "2", "Shows", "show", 0),
    ],
    "media_items": [
        (1, 1, "100", None, "Tenet", "movie", None, "2024-04-01T00:00:00+00:00",
         "2024-04-01T00:00:00+00:00", 7.3, "plex", None,
         "/media/Movies/Tenet/Tenet.mkv", 25769803776, None, None, None, None,
         None, None, None, None, None, 1,
         "/library/metadata/100/thumb/1700000000", None, None),
        (2, 1, "101", None, "The Gray Man", "movie", None,
         "2024-04-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00", 7.3, "plex",
         None, "/media/Movies/The Gray Man/The Gray Man.mkv", 25769803776,
         None, None, None, None, None, None, None, None, None, 1,
         "/library/metadata/101/thumb/1700000000", None, None),
        (3, 2, "200", None, "Lost in Space", "series", None,
         "2024-04-01T00:00:00+00:00", None, None, None, None, None, None, None,
         None, None, None, None, None, None, None, None, 1,
         "/library/metadata/200/thumb/1700000000", None, None),
        (4, 2, "210", "200", "Season 1", "season", 1, None, None, None, None,
         None, None, None, None, None, None, None, None, None, None, None,
         None, 1, None, None, None),
        (5, 2, "211", "200", "Season 2", "season", 2, None, None, None, None,
         None, None, None, None, None, None, None, None, None, None, None,
         None, 1, None, None, None),
    ],
    "episodes": [
        (1, 4, "300", "200", 1, 1, "Episode 1", "/media/TV/210/1.mkv",
         2147483648, None, None, None, 1),
        (2, 4, "301", "200", 1, 2, "Episode 2", "/media/TV/210/2.mkv",
         2147483648, None, None, None, 1),
    ],
    "users": [
        (1, "1", "Marc", 0, 0),
        (2, "2", "Anna", 0, 0),
    ],
    "play_events": [
        (1, 1, "100", 1, "2023-11-14T22:13:20+00:00", 6000000, 6000000, "plex"),
        (2, 2, "200", 0, "2020-09-13T12:26:40+00:00", 60000, 2700000, "plex"),
    ],
}


def test_the_sync_stores_the_same_rows_as_before_the_optimisations(server, conn):
    PlexSync(build_client(server), conn).run()

    assert snapshot(conn) == EXPECTED


def test_a_second_sync_stores_the_same_rows(server, conn):
    PlexSync(build_client(server), conn).run()
    PlexSync(build_client(server), conn).run()

    assert snapshot(conn) == EXPECTED
