import pytest

from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_plex_server import FakePlexDataset, FakePlexServer, movie, series
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource


def sync(conn, movies=(), shows=()):
    data = FakePlexDataset()
    data.items = {"1": list(movies), "2": list(shows)}
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()


def test_link_arr_uses_external_ids_and_monitored(conn):
    from almali_library_curator.enrichment import link_arr
    sync(conn, [movie("100", "Wrong title", Guid=[{"id": "tmdb://42"}])],
         [series("200", "Another title", Guid=[{"id": "tvdb://84"}])])
    radarr_data = FakeRadarrDataset()
    radarr_data.movies = [movie_resource(10, "Tenet", tmdbId=42, monitored=False)]
    sonarr_data = FakeSonarrDataset()
    sonarr_data.series = [series_resource(20, "Show", tvdbId=84)]
    with FakeRadarrServer(radarr_data) as r, FakeSonarrServer(sonarr_data) as s:
        summary = link_arr(conn, radarr=RadarrClient(r.base_url, "test-api-key", rate_limit_per_second=0),
                           sonarr=SonarrClient(s.base_url, "test-api-key", rate_limit_per_second=0))
        assert all(request["method"] == "GET" for request in r.requests + s.requests)
    assert summary.radarr_linked == summary.sonarr_linked == 1
    assert tuple(conn.execute("SELECT radarr_id, monitored FROM media_items WHERE plex_rating_key='100'").fetchone()) == (10, 0)
    assert tuple(conn.execute("SELECT sonarr_id, monitored FROM media_items WHERE plex_rating_key='200'").fetchone()) == (20, 1)
    from almali_library_curator.enrichment import build_enrichers
    from almali_library_curator.detection.enrichers import SeriesStatus
    assert build_enrichers(conn).series_status.series_status("Another title", "200").status is SeriesStatus.ENDED


@pytest.mark.parametrize("guids,title,year,resources,expected,ambiguous", [
    # IMDb is the secondary movie identity, even when a TMDB id has no match.
    (["tmdb://999", "imdb://tt42"], "Other", 2020,
     [movie_resource(10, "Tenet", imdbId="tt42")], 10, 0),
    # TMDB wins over a conflicting IMDb match.
    (["tmdb://42", "imdb://tt42"], "Other", 2020,
     [movie_resource(10, "Tenet", tmdbId=42), movie_resource(11, "Other", imdbId="tt42")], 10, 0),
    ([], "TENET", 2020, [movie_resource(10, "Tenet")], 10, 0),
    ([], "Tenet", 2021, [movie_resource(10, "Tenet")], None, 0),
    ([], "Tenet", None, [movie_resource(10, "Tenet")], None, 0),
    (["tmdb://999"], "Tenet", 2020, [movie_resource(10, "Tenet")], None, 0),
    (["tvdb://999"], "Tenet", 2020, [movie_resource(10, "Tenet")], None, 0),
    ([], "Tenet", 2020, [movie_resource(10, "Tenet"), movie_resource(11, "Tenet")], None, 1),
    (["tmdb://42", "imdb://tt42"], "Tenet", 2020,
     [movie_resource(10, "Tenet", tmdbId=42), movie_resource(11, "Tenet", tmdbId=42)], None, 1),
])
def test_movie_matching_is_conservative(conn, guids, title, year, resources, expected, ambiguous):
    from almali_library_curator.enrichment import link_arr
    sync(conn, [movie("100", title, year=year, Guid=[{"id": guid} for guid in guids])])
    data = FakeRadarrDataset()
    data.movies = resources
    with FakeRadarrServer(data) as server:
        client = RadarrClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        summary = link_arr(conn, radarr=client)
    assert conn.execute("SELECT radarr_id FROM media_items").fetchone()[0] == expected
    assert summary.ambiguous == ambiguous


def test_sonarr_title_year_fallback_and_stale_link_clear(conn):
    from almali_library_curator.enrichment import link_arr
    sync(conn, shows=[series("200", "SHOW", year=2018)])
    data = FakeSonarrDataset()
    data.series = [series_resource(20, "Show")]
    with FakeSonarrServer(data) as server:
        client = SonarrClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert link_arr(conn, sonarr=client).sonarr_linked == 1
        data.series.append(series_resource(21, "Show"))
        assert link_arr(conn, sonarr=client).ambiguous == 1
        assert tuple(conn.execute("SELECT sonarr_id, monitored FROM media_items").fetchone()) == (None, None)
    assert link_arr(conn).skipped_services == ("sonarr", "radarr")


def test_failed_service_rolls_back_its_links_and_counts_without_starving_the_other(conn):
    from almali_library_curator.enrichment import link_arr
    sync(conn, [movie("100", "Movie", Guid=[{"id": "tmdb://42"}])],
         [series("200", "Show", Guid=[{"id": "tvdb://84"}])])
    conn.execute("CREATE TRIGGER reject_sonarr BEFORE UPDATE OF sonarr_id ON media_items "
                 "BEGIN SELECT RAISE(ABORT, 'sensitive-diagnostic'); END")
    radarr_data = FakeRadarrDataset()
    radarr_data.movies = [movie_resource(10, "Movie", tmdbId=42)]
    sonarr_data = FakeSonarrDataset()
    sonarr_data.series = [series_resource(20, "Show", tvdbId=84)]
    with FakeRadarrServer(radarr_data) as r, FakeSonarrServer(sonarr_data) as s:
        summary = link_arr(conn,
            radarr=RadarrClient(r.base_url, "test-api-key", rate_limit_per_second=0),
            sonarr=SonarrClient(s.base_url, "test-api-key", rate_limit_per_second=0))
    assert summary.radarr_linked == 1
    assert summary.sonarr_linked == 0
    assert summary.errors == {"sonarr": "IntegrityError"}
    assert conn.execute("SELECT sonarr_id FROM media_items WHERE plex_rating_key='200'").fetchone()[0] is None
