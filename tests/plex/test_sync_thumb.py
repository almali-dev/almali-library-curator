"""S6 (#139): the sync stores the poster thumb Plex sends, as it sent it.

The stored value is raw: whether it may become a poster request is decided
when the card renders and when the poster route reads it (the shared
``is_poster_thumb`` rule), never by rewriting what the server said.
"""

from __future__ import annotations

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    movie,
    season,
    series,
)

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync

MOVIE_THUMB = "/library/metadata/100/thumb/1700000000"
SERIES_THUMB = "/library/metadata/200/thumb/1700000001"
SEASON_THUMB = "/library/metadata/210/thumb/1700000002"


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet", thumb=MOVIE_THUMB)]
    data.items["2"] = [series("200", "Lost in Space", thumb=SERIES_THUMB)]
    data.children["200"] = [season("210", "200", 1, thumb=SEASON_THUMB)]
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


def stored_thumbs(conn) -> dict[str, str | None]:
    return {
        row["plex_rating_key"]: row["thumb"]
        for row in conn.execute("SELECT plex_rating_key, thumb FROM media_items")
    }


def test_the_sync_stores_the_thumb_of_a_movie_a_series_and_a_season(server, conn):
    PlexSync(build_client(server), conn).run()

    assert stored_thumbs(conn) == {
        "100": MOVIE_THUMB,
        "200": SERIES_THUMB,
        "210": SEASON_THUMB,
    }


def test_an_item_plex_sends_without_a_thumb_stores_none(server, conn):
    server.dataset.items["1"] = [movie("100", "Tenet", thumb=None)]

    PlexSync(build_client(server), conn).run()

    assert stored_thumbs(conn)["100"] is None


@pytest.mark.parametrize("sent", [
    "https://evil.example/x.jpg",
    "/library/metadata/100/thumb/1700000000?X-Plex-Token=token",
    "/photo/:/transcode?url=/library/metadata/100/thumb/1",
])
def test_the_sync_stores_what_plex_sent_and_validates_nothing(server, conn, sent):
    """S3: validation happens when the card renders, never in the sync."""
    server.dataset.items["1"] = [movie("100", "Tenet", thumb=sent)]

    PlexSync(build_client(server), conn).run()

    assert stored_thumbs(conn)["100"] == sent
