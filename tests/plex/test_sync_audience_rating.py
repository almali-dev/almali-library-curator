"""#128: the Plex sync stores the effective rating and where it came from.

Plex's critic ``rating`` wins; when it is absent, ``audienceRating`` is the
effective number, stored in the existing ``rating``/``rating_source`` columns.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [
        movie("100", "Critic", rating=7.3, audienceRating=9.1),
        movie("101", "Audience", rating=None, audienceRating=8.1),
        movie("102", "Unrated", rating=None, audienceRating=None),
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


def synced_rows(server, conn):
    client = PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=100,
        rate_limit_per_second=1000.0,
        backoff_seconds=0.0,
    )
    PlexSync(client, conn).run()
    return {
        row["title"]: (row["rating"], row["rating_source"])
        for row in conn.execute("SELECT title, rating, rating_source FROM media_items")
    }


def test_the_critic_rating_wins_over_the_audience_rating(server, conn):
    assert synced_rows(server, conn)["Critic"] == (7.3, "plex")


def test_the_audience_rating_is_the_effective_rating_when_the_critic_one_is_absent(server, conn):
    assert synced_rows(server, conn)["Audience"] == (8.1, "plex_audience")


def test_an_unrated_item_stores_no_rating_and_no_source(server, conn):
    assert synced_rows(server, conn)["Unrated"] == (None, None)
