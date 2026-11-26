"""#128 S2: the sync stores the Plex number the Rating source setting chooses.

The default ``critic`` keeps only Plex's critic ``rating`` (the behaviour
released before #128); ``audience`` stores the audience rating first and the
critic one second; ``highest`` stores the larger of the two. TMDB is a
detection-time fallback and is never stored by the sync.
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
        movie("100", "Both ratings", rating=7.3, audienceRating=9.1),
        movie("101", "Audience only", rating=None, audienceRating=8.1),
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


def synced_rows(server, conn, rating_source="critic"):
    client = PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=100,
        rate_limit_per_second=1000.0,
        backoff_seconds=0.0,
    )
    PlexSync(client, conn, rating_source=rating_source).run()
    return {
        row["title"]: (row["rating"], row["rating_source"])
        for row in conn.execute("SELECT title, rating, rating_source FROM media_items")
    }


def test_the_default_keeps_the_critic_rating_and_ignores_the_audience_one(server, conn):
    rows = synced_rows(server, conn)

    assert rows["Both ratings"] == (7.3, "plex")
    assert rows["Audience only"] == (None, None)


def test_the_audience_setting_prefers_the_audience_rating_even_when_the_critic_one_exists(
        server, conn):
    rows = synced_rows(server, conn, rating_source="audience")

    assert rows["Both ratings"] == (9.1, "plex_audience")
    assert rows["Audience only"] == (8.1, "plex_audience")


def test_the_highest_setting_keeps_the_larger_plex_rating(server, conn):
    rows = synced_rows(server, conn, rating_source="highest")

    assert rows["Both ratings"] == (9.1, "plex_audience")
    assert rows["Audience only"] == (8.1, "plex_audience")


def test_an_unrated_item_stores_no_rating_and_no_source(server, conn):
    assert synced_rows(server, conn)["Unrated"] == (None, None)
