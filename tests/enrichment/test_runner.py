from dataclasses import asdict, replace
import json

import pytest

from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.tautulli import TautulliClient
from almali_library_curator.tmdb import TmdbClient
from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie


def setup_movie(conn, path):
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Movie", rating=None, Guid=[{"id": "tmdb://42"}],
                             Part=[{"file": str(path), "size": 99}])]
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()


def test_run_enrichment_orders_link_files_history_and_exposes_detection_adapters(conn, settings, tmp_path):
    from almali_library_curator.enrichment import run_enrichment
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"movie")
    setup_movie(conn, path)
    radarr = FakeRadarrDataset()
    radarr.movies = [movie_resource(10, "Movie", tmdbId=42)]
    tautulli = FakeTautulliDataset()
    tautulli.history = [history_row(1, 100, 1, "Marc")]
    tmdb = FakeTmdbDataset()
    tmdb.movies[42] = tmdb_movie(42, "Movie", vote_average=8.4)
    statements = []
    conn.set_trace_callback(statements.append)
    with FakeRadarrServer(radarr) as r, FakeTautulliServer(tautulli) as t, FakeTmdbServer(tmdb) as m:
        summary = run_enrichment(conn, settings,
            radarr=RadarrClient(r.base_url, "test-api-key", rate_limit_per_second=0),
            tautulli=TautulliClient(t.base_url, "test-api-key", rate_limit_per_second=0),
            tmdb=TmdbClient(m.base_url, "test-access-token", rate_limit_per_second=0),
            hostfs=HostFilesystem(media_root=tmp_path))
        assert summary.enrichers.rating.rating("Movie", "movie", "100") == 8.4
    assert summary.linking.radarr_linked == 1
    assert summary.files.movies == summary.history_imported == 1
    assert summary.errors == {}
    linked = next(i for i, sql in enumerate(statements) if "SET radarr_id=" in sql)
    refreshed = next(i for i, sql in enumerate(statements) if "SET file_size_bytes=" in sql)
    history = next(i for i, sql in enumerate(statements) if "INSERT INTO play_events" in sql)
    assert linked < refreshed < history
    assert "test-api-key" not in json.dumps(asdict(summary))


@pytest.mark.parametrize("failure", ["link_arr", "refresh_files", "import_tautulli_history"])
def test_run_enrichment_failure_is_recorded_without_aborting_other_steps(conn, settings, tmp_path, caplog, failure):
    from almali_library_curator.enrichment import run_enrichment
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"movie")
    setup_movie(conn, path)
    radarr = FakeRadarrDataset()
    radarr.movies = [movie_resource(10, "Movie", tmdbId=42)]
    tautulli = FakeTautulliDataset()
    tautulli.history = [history_row(1, 100, 1, "Marc")]
    if failure == "link_arr":
        radarr.fail_always("GET /api/v3/movie", 503)
    elif failure == "refresh_files":
        conn.execute("CREATE TRIGGER fail_file_refresh BEFORE UPDATE OF hardlink_count ON media_items "
                     "BEGIN SELECT RAISE(ABORT, 'sensitive-diagnostic'); END")
    else:
        tautulli.fail_command("get_history", "rejected test-api-key sensitive-diagnostic")
    with FakeRadarrServer(radarr) as r, FakeTautulliServer(tautulli) as t:
        summary = run_enrichment(conn, settings,
            radarr=RadarrClient(r.base_url, "test-api-key", rate_limit_per_second=0, max_retries=0),
            tautulli=TautulliClient(t.base_url, "test-api-key", rate_limit_per_second=0, max_retries=0),
            hostfs=HostFilesystem(media_root=tmp_path))
        assert any(request["cmd"] == "get_history" for request in t.requests)
    assert any(key.startswith(failure) for key in summary.errors)
    if failure != "link_arr":
        assert summary.linking.radarr_linked == 1
    if failure != "refresh_files":
        assert summary.files.movies == 1
    if failure != "import_tautulli_history":
        assert summary.history_imported == 1
    assert "test-api-key" not in repr(summary) + caplog.text + repr([r.__dict__ for r in caplog.records])
    assert "sensitive-diagnostic" not in repr(summary)


def test_unconfigured_services_degrade_and_configured_services_are_constructed(conn, settings, tmp_path):
    from almali_library_curator.enrichment import run_enrichment
    assert run_enrichment(conn, settings).errors == {}
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"movie")
    setup_movie(conn, path)
    data = FakeRadarrDataset()
    data.movies = [movie_resource(10, "Movie", tmdbId=42)]
    with FakeRadarrServer(data) as server:
        configured = replace(settings, radarr_base_url=server.base_url, radarr_api_key="test-api-key")
        summary = run_enrichment(conn, configured)
    assert summary.linking.radarr_linked == 1
    assert summary.files.movies == 1
    assert summary.history_imported == 0


def test_a_refused_tautulli_row_is_named_by_model_field_and_type(conn, settings, tmp_path, caplog):
    """#120: the enrichment diagnostic names the refused field, never its value."""
    import logging
    from almali_library_curator.enrichment import run_enrichment
    tautulli = FakeTautulliDataset()
    refused = history_row(1, 100, 1, "Marc")
    refused["rating_key"] = "secret-value"
    tautulli.history = [refused]
    with FakeTautulliServer(tautulli) as t, caplog.at_level(logging.WARNING):
        summary = run_enrichment(conn, settings,
            tautulli=TautulliClient(t.base_url, "test-api-key", rate_limit_per_second=0, max_retries=0),
            hostfs=HostFilesystem(media_root=tmp_path))
    expected = "ValidationError: TautulliHistoryEntry rating_key int_parsing"
    assert summary.errors["import_tautulli_history"] == expected
    record = next(r for r in caplog.records if r.msg == "enrichment_step_failed")
    assert (record.step, record.error_type, record.error) == (
        "import_tautulli_history", "ValidationError", expected)
    assert "secret-value" not in repr(summary) + caplog.text
