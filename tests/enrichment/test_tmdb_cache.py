"""#74: TMDB answers are kept across scans, and nobody is asked twice.

The old cache was a dictionary of one scan, so 2,500 linked series meant
2,500 reads every scan (over eight minutes at five requests per second).
The answers now live in ``tmdb_cache`` with a TTL, and a series whose
status Sonarr already gave is not read at all when TMDB has nothing left
to add.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.tmdb import TmdbClient
from fake_plex_server import FakePlexDataset, FakePlexServer, movie, season, series
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie, tmdb_show

SHOW_PATH = "/3/tv/84"
MOVIE_PATH = "/3/movie/42"


@pytest.fixture()
def conn(tmp_path):
    from almali_library_curator.db import init_db

    connection = init_db(tmp_path / "curator.db")
    yield connection
    connection.close()


def sync(conn, series_rating: float | None = None) -> None:
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Movie", rating=None, Guid=[{"id": "tmdb://42"}])]
    extra = {"Guid": [{"id": "tmdb://84"}]}
    if series_rating is not None:
        extra["rating"] = series_rating
    data.items["2"] = [series("200", "Show", **extra)]
    data.children["200"] = [season("210", "200", 1)]
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()


def dataset() -> FakeTmdbDataset:
    data = FakeTmdbDataset()
    data.movies[42] = tmdb_movie(42, "Movie", vote_average=8.4)
    data.shows[84] = tmdb_show(84, "Show", status="Ended", last_air_date="2022-12-01")
    return data


def client_for(server) -> TmdbClient:
    return TmdbClient(server.base_url, "test-access-token", rate_limit_per_second=0,
                      max_retries=0)


def test_tmdb_answers_survive_across_scans(conn):
    from almali_library_curator.enrichment import build_enrichers

    sync(conn)
    data = dataset()
    with FakeTmdbServer(data) as server:
        first = build_enrichers(conn, tmdb=client_for(server))
        assert first.series_status.series_status("Show", "200").status.value == "ended"
        assert first.rating.rating("Movie", "movie", "100") == 8.4

        second = build_enrichers(conn, tmdb=client_for(server))
        info = second.series_status.series_status("Show", "200")
        assert (info.status.value, info.ended_year, info.rating) == ("ended", 2022, 8.1)
        assert second.rating.rating("Movie", "movie", "100") == 8.4

        assert len(server.requests_for(SHOW_PATH)) == 1
        assert len(server.requests_for(MOVIE_PATH)) == 1
    assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 2


def test_a_stale_tmdb_answer_is_fetched_again(conn):
    from almali_library_curator.enrichment import build_enrichers

    sync(conn)
    data = dataset()
    with FakeTmdbServer(data) as server:
        build_enrichers(conn, tmdb=client_for(server)).rating.rating("Movie", "movie", "100")
        stale = (datetime.now(tz=timezone.utc) - timedelta(days=400)).isoformat()
        conn.execute("UPDATE tmdb_cache SET fetched_at = ?", (stale,))
        conn.commit()

        data.movies[42] = tmdb_movie(42, "Movie", vote_average=9.1)
        assert build_enrichers(conn, tmdb=client_for(server)).rating.rating(
            "Movie", "movie", "100") == 9.1
        assert len(server.requests_for(MOVIE_PATH)) == 2


def test_a_failed_tmdb_answer_is_not_stored(conn):
    from almali_library_curator.enrichment import build_enrichers

    sync(conn)
    data = dataset()
    with FakeTmdbServer(data) as server:
        data.fail_once(MOVIE_PATH, 503)
        assert build_enrichers(conn, tmdb=client_for(server)).rating.rating(
            "Movie", "movie", "100") is None
        assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 0

        assert build_enrichers(conn, tmdb=client_for(server)).rating.rating(
            "Movie", "movie", "100") == 8.4
        assert len(server.requests_for(MOVIE_PATH)) == 2


def test_a_known_sonarr_status_needs_no_tmdb_read_when_plex_already_rated_the_series(conn):
    from almali_library_curator.enrichment import build_enrichers

    sync(conn, series_rating=8.2)
    conn.execute("UPDATE media_items SET production_status = 'continuing'"
                 " WHERE plex_rating_key = '200'")
    conn.commit()
    data = dataset()
    with FakeTmdbServer(data) as server:
        info = build_enrichers(conn, tmdb=client_for(server)).series_status.series_status(
            "Show", "200")
        assert server.requests_for(SHOW_PATH) == []
    assert (info.status.value, info.ended_year, info.rating) == ("continuing", None, None)


def test_an_ended_series_keeps_the_year_the_reason_needs(conn):
    """The Sonarr status is enough for the category, not for its sentence."""
    from almali_library_curator.enrichment import build_enrichers

    sync(conn, series_rating=8.2)
    conn.execute("UPDATE media_items SET production_status = 'ended'"
                 " WHERE plex_rating_key = '200'")
    conn.commit()
    data = dataset()
    with FakeTmdbServer(data) as server:
        info = build_enrichers(conn, tmdb=client_for(server)).series_status.series_status(
            "Show", "200")
        assert len(server.requests_for(SHOW_PATH)) == 1
    assert (info.status.value, info.ended_year) == ("ended", 2022)


def test_an_unrated_series_still_gets_the_tmdb_rating(conn):
    """Without it the high-rating protection could not protect the series."""
    from almali_library_curator.enrichment import build_enrichers

    sync(conn)
    conn.execute("UPDATE media_items SET production_status = 'continuing'"
                 " WHERE plex_rating_key = '200'")
    conn.commit()
    data = dataset()
    with FakeTmdbServer(data) as server:
        info = build_enrichers(conn, tmdb=client_for(server)).series_status.series_status(
            "Show", "200")
        assert len(server.requests_for(SHOW_PATH)) == 1
    assert (info.status.value, info.rating) == ("continuing", 8.1)
