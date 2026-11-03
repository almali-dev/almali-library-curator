from datetime import datetime, timezone

import pytest

from almali_library_curator.detection import DetectionEngine
from almali_library_curator.detection.enrichers import SeriesStatus
from almali_library_curator.detection.models import Category
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.tmdb import TmdbClient
from fake_plex_server import FakePlexDataset, FakePlexServer, movie, season, series
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie, tmdb_show


def sync(conn):
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Movie", rating=None, Guid=[{"id": "tmdb://42"}]),
                       movie("101", "Plex rated", rating=7.3, Guid=[{"id": "tmdb://43"}])]
    data.items["2"] = [series("200", "Show", Guid=[{"id": "tmdb://84"}])]
    data.children["200"] = [season("210", "200", 1)]
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()


@pytest.mark.parametrize("status,expected", [("Canceled", SeriesStatus.CANCELLED),
    ("Ended", SeriesStatus.ENDED), ("Returning Series", SeriesStatus.CONTINUING),
    ("In Production", SeriesStatus.CONTINUING), ("Unknown status", SeriesStatus.UNKNOWN)])
def test_tmdb_enrichers_supply_detection_and_inherit_season_identity(conn, settings, status, expected):
    from almali_library_curator.enrichment import build_enrichers
    sync(conn)
    data = FakeTmdbDataset()
    data.movies[42] = tmdb_movie(42, "Movie", vote_average=8.4)
    data.movies[43] = tmdb_movie(43, "Plex rated", vote_average=9.9)
    data.shows[84] = tmdb_show(84, "Show", status=status, last_air_date="2022-12-01")
    with FakeTmdbServer(data) as server:
        enrichers = build_enrichers(conn, tmdb=TmdbClient(server.base_url, "test-access-token", rate_limit_per_second=0))
        info = enrichers.series_status.series_status("Show", "200")
        assert info.status == expected
        assert info.ended_year == (2022 if expected in (SeriesStatus.CANCELLED, SeriesStatus.ENDED) else None)
        assert enrichers.rating.rating("Season 1", "season", "210") == 8.1
        candidates = DetectionEngine(conn, enrichers=enrichers, settings=settings,
            clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc)).scan()
        by_key = {c.plex_rating_key: c for c in candidates}
        assert by_key["100"].rating == 8.4
        assert by_key["101"].rating == 7.3
        assert (Category.CANCELLED_NO_VIEWERS in by_key["200"].categories) == (
            expected in (SeriesStatus.ENDED, SeriesStatus.CANCELLED))
        assert len([r for r in server.requests if r["path"] == "/3/tv/84"]) == 1
        assert not [r for r in server.requests if r["path"] == "/3/movie/43"]


def test_tmdb_settings_use_the_public_default_when_no_base_override(settings):
    client = TmdbClient.from_settings(settings)
    assert client.base_url == "https://api.themoviedb.org"


def test_tmdb_failure_and_absent_ids_degrade_and_sonarr_status_takes_priority(conn, caplog):
    from almali_library_curator.enrichment import build_enrichers
    sync(conn)
    conn.execute("UPDATE media_items SET production_status='continuing' WHERE plex_rating_key='200'")
    data = FakeTmdbDataset()
    data.shows[84] = tmdb_show(84, "Show", status="Ended")
    data.fail_always("/3/movie/42", 503)
    with FakeTmdbServer(data) as server:
        enrichers = build_enrichers(conn, tmdb=TmdbClient(server.base_url, "test-access-token", max_retries=0, rate_limit_per_second=0))
        assert enrichers.series_status.series_status("Show", "200").status is SeriesStatus.CONTINUING
        assert enrichers.rating.rating("Movie", "movie", "100") is None
        assert enrichers.rating.rating("Unknown", "movie", "999") is None
    absent = build_enrichers(conn)
    assert absent.rating.rating("Movie", "movie", "100") is None
    assert absent.series_status.series_status("Show", "200").status is SeriesStatus.CONTINUING
    assert "test-access-token" not in caplog.text
