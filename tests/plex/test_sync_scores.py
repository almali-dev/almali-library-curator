"""#162 S3: the sync keeps the IMDb score and the Tomatometer Plex sends.

Only what Plex sends: its ``Rating`` entries when present, else the
``ratingImage``/``audienceRatingImage`` attributes beside ``rating`` and
``audienceRating``. Any other source is ignored, and nothing is invented.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie, series

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


def scores(conn, key):
    row = conn.execute(
        "SELECT imdb_rating, rt_critic FROM media_items WHERE plex_rating_key = ?", (key,)
    ).fetchone()
    return tuple(row)


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [
        # The new agents: every source as a Rating entry.
        movie("100", "Tenet", Rating=[
            {"image": "imdb://image.rating", "value": 7.3, "type": "audience"},
            {"image": "rottentomatoes://image.rating.ripe", "value": 6.9, "type": "critic"},
            {"image": "rottentomatoes://image.rating.upright", "value": 7.6, "type": "audience"},
            {"image": "themoviedb://image.rating", "value": 7.2, "type": "audience"},
        ]),
        # Only the attributes: Rotten Tomatoes as the library's source.
        movie("101", "Cats", rating=1.9, ratingImage="rottentomatoes://image.rating.rotten",
              audienceRating=5.3, audienceRatingImage="rottentomatoes://image.rating.spilled"),
        # Only the attributes: IMDb as the audience source.
        movie("102", "Heat", audienceRating=8.3, audienceRatingImage="imdb://image.rating"),
        # Another source only: nothing is kept.
        movie("103", "Up", rating=8.0, ratingImage="themoviedb://image.rating"),
    ]
    data.items["2"] = [series("200", "Lost in Space", Rating=[
        {"image": "imdb://image.rating", "value": 7.3, "type": "audience"}])]
    return data


@pytest.fixture()
def conn(tmp_path, dataset):
    db = init_db(tmp_path / "curator.db")
    with FakePlexServer(dataset) as server:
        client = PlexClient(base_url=server.base_url, token="test-token",
                            rate_limit_per_second=1000.0, backoff_seconds=0.0)
        PlexSync(client, db).run()
    yield db
    db.close()


def test_the_rating_entries_give_the_imdb_score_and_the_tomatometer(conn):
    assert scores(conn, "100") == (7.3, 69)


def test_the_rating_attributes_name_their_source(conn):
    assert scores(conn, "101") == (None, 19)
    assert scores(conn, "102") == (8.3, None)


def test_another_source_is_never_taken_for_imdb_or_rotten_tomatoes(conn):
    assert scores(conn, "103") == (None, None)


def test_a_series_keeps_its_imdb_score(conn):
    assert scores(conn, "200") == (7.3, None)
