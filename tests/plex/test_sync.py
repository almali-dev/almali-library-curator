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
from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet"), movie("101", "The Gray Man")]
    data.items["2"] = [series("200", "Lost in Space")]
    data.children["200"] = [season("210", "200", 1), season("211", "200", 2)]
    data.children["210"] = [episode("300", "200", "210", 1), episode("301", "200", "210", 2)]
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


def test_sync_stores_the_library_the_detection_engine_needs(server, conn):
    summary = PlexSync(build_client(server), conn).run()

    assert summary.libraries == 2
    assert summary.media_items == 5
    assert summary.episodes == 2
    assert summary.users == 2
    assert summary.play_events == 2

    rows = conn.execute(
        "SELECT item_type, title FROM media_items ORDER BY item_type, title"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("movie", "Tenet"),
        ("movie", "The Gray Man"),
        ("season", "Season 1"),
        ("season", "Season 2"),
        ("series", "Lost in Space"),
    ]

    series_row = conn.execute(
        "SELECT parent_rating_key, season_number FROM media_items WHERE item_type = 'season'"
    ).fetchone()
    assert series_row["parent_rating_key"] == "200"

    episode_row = conn.execute(
        "SELECT series_rating_key, season_number, episode_number FROM episodes"
    ).fetchone()
    assert episode_row["series_rating_key"] == "200"
    assert episode_row["season_number"] == 1
    assert episode_row["episode_number"] == 1

    history_rows = conn.execute(
        "SELECT watched, position_ms, duration_ms FROM play_events ORDER BY position_ms"
    ).fetchall()
    assert [row["watched"] for row in history_rows] == [0, 1]


def test_sync_is_idempotent(server, conn):
    PlexSync(build_client(server), conn).run()
    PlexSync(build_client(server), conn).run()

    counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("libraries", "media_items", "episodes", "users", "play_events")
    }

    assert counts == {
        "libraries": 2,
        "media_items": 5,
        "episodes": 2,
        "users": 2,
        "play_events": 2,
    }


def test_sync_requests_external_guids_and_stores_ids_and_year(dataset, server, conn):
    dataset.items["1"][0].update(
        year=2020,
        Guid=[{"id": "tmdb://577922"}, {"id": "imdb://tt6723592"}],
    )
    dataset.items["2"][0].update(
        year=2018,
        Guid=[{"id": "tvdb://343253"}, {"id": "tmdb://75758"}],
    )
    PlexSync(build_client(server), conn).run()
    row = conn.execute(
        "SELECT tmdb_id, tvdb_id, imdb_id, year FROM media_items WHERE plex_rating_key='100'"
    ).fetchone()
    assert tuple(row) == (577922, None, "tt6723592", 2020)
    row = conn.execute(
        "SELECT tmdb_id, tvdb_id, year FROM media_items WHERE plex_rating_key='200'"
    ).fetchone()
    assert tuple(row) == (75758, 343253, 2018)
    # Seasons resolve their external identity through the stored parent key.
    row = conn.execute(
        "SELECT p.tvdb_id FROM media_items s JOIN media_items p "
        "ON p.plex_rating_key=s.parent_rating_key WHERE s.plex_rating_key='210'"
    ).fetchone()
    assert row[0] == 343253
    requests = [r for r in server.requests if r["path"].endswith("/all")
                and r["path"].startswith("/library/sections/")]
    assert requests and all(r["query"].get("includeGuids") == "1" for r in requests)


def test_sync_refreshes_ids_and_year_and_ignores_malformed_guids(dataset, server, conn):
    item = dataset.items["1"][0]
    item.update(year=2020, Guid=[{"id": "tmdb://42"}, {"id": "tvdb://not-a-number"},
                                {"id": "imdb://not-an-id"}, {"id": "plex://movie/42"}])
    PlexSync(build_client(server), conn).run()
    row = conn.execute("SELECT tmdb_id, tvdb_id, imdb_id, year FROM media_items "
                       "WHERE plex_rating_key='100'").fetchone()
    assert tuple(row) == (42, None, None, 2020)
    item.update(year=2021, Guid=[])
    PlexSync(build_client(server), conn).run()
    row = conn.execute("SELECT tmdb_id, year FROM media_items WHERE plex_rating_key='100'").fetchone()
    assert tuple(row) == (None, 2021)


def test_sync_records_the_plex_reported_path_and_size(server, conn):
    PlexSync(build_client(server), conn).run()

    row = conn.execute(
        "SELECT file_path, file_size_bytes FROM media_items WHERE title = 'Tenet'"
    ).fetchone()

    assert row["file_path"] == "/media/Movies/Tenet/Tenet.mkv"
    assert row["file_size_bytes"] == 24 * 1024**3
