"""#133 T3: which file Radarr manages for a movie, read-only.

S3: when the version the owner removes is the file Radarr manages, the
removal is refused and Radarr is never changed. That refusal needs this
read, and a read that cannot answer must raise, never answer "no file".
"""

from __future__ import annotations

import pytest

from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from almali_library_curator.radarr import RadarrClient, RadarrError, RadarrNotFound

MANAGED = "/movies/Tenet/Tenet (2020) Bluray-1080p.mkv"


def client_for(server) -> RadarrClient:
    return RadarrClient(base_url=server.base_url, api_key="test-api-key",
                        rate_limit_per_second=1000.0, backoff_seconds=0.0, max_retries=0)


def serve(*movies):
    dataset = FakeRadarrDataset()
    dataset.movies.extend(movies)
    return dataset, FakeRadarrServer(dataset)


def test_the_managed_file_is_the_path_radarr_reports():
    _, running = serve(movie_resource(1, "Tenet", movieFile={
        "id": 7, "path": MANAGED, "relativePath": "Tenet (2020) Bluray-1080p.mkv"}))
    with running as server:
        assert client_for(server).managed_file(1) == MANAGED
        assert [(r["method"], r["path"]) for r in server.requests] == [
            ("GET", "/api/v3/movie/1")]


def test_a_movie_without_a_file_has_no_managed_file():
    _, running = serve(movie_resource(1, "Tenet", hasFile=False))
    with running as server:
        assert client_for(server).managed_file(1) is None


def test_a_file_radarr_claims_but_does_not_describe_is_an_error_not_none():
    # hasFile without a movieFile path cannot prove which file is managed.
    _, running = serve(movie_resource(1, "Tenet", hasFile=True))
    with running as server:
        with pytest.raises(RadarrError):
            client_for(server).managed_file(1)


def test_a_failed_read_raises():
    dataset, running = serve(movie_resource(1, "Tenet", movieFile={"path": MANAGED}))
    dataset.fail_always("GET /api/v3/movie/1", 503)
    with running as server:
        with pytest.raises(RadarrError):
            client_for(server).managed_file(1)


def test_a_missing_movie_raises_not_found():
    _, running = serve()
    with running as server:
        with pytest.raises(RadarrNotFound):
            client_for(server).managed_file(1)


def test_the_read_never_writes():
    _, running = serve(movie_resource(1, "Tenet", movieFile={"path": MANAGED}))
    with running as server:
        client_for(server).managed_file(1)
        assert all(r["method"] == "GET" for r in server.requests)
