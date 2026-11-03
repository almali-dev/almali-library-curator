from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from almali_library_curator.radarr import (
    RadarrAuthError,
    RadarrClient,
    RadarrError,
    RadarrUnavailableError,
)

STATUS = "GET /api/v3/system/status"


@pytest.fixture()
def dataset() -> FakeRadarrDataset:
    return FakeRadarrDataset()


@pytest.fixture()
def server(dataset):
    with FakeRadarrServer(dataset) as running:
        yield running


def build_client(server, **overrides) -> RadarrClient:
    options = {
        "base_url": server.base_url,
        "api_key": "test-api-key",
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return RadarrClient(**options)


def test_sends_the_api_key_in_the_header_and_never_in_the_url(server):
    client = build_client(server)

    client.system_status()

    request = server.requests_for("/api/v3/system/status")[0]
    assert request["headers"]["X-Api-Key"] == "test-api-key"
    assert "test-api-key" not in request["path"]
    assert "test-api-key" not in request["query"]


def test_system_status_reports_the_app_version(server):
    status = build_client(server).system_status()

    assert status.appName == "Radarr"
    assert status.version == "5.30.0.9730"


def test_fetches_one_movie_by_id(server, dataset):
    dataset.movies = [movie_resource(7, "Tenet", monitored=True)]

    entry = build_client(server).movie(7)

    assert entry.id == 7
    assert entry.title == "Tenet"
    assert entry.tmdbId == 2007
    assert entry.monitored is True
    assert entry.hasFile is True
    assert entry.sizeOnDisk == 24 * 1024**3


def test_a_wrong_api_key_raises_an_auth_error(server):
    client = build_client(server, api_key="wrong-key")

    with pytest.raises(RadarrAuthError):
        client.system_status()


def test_a_transient_failure_is_retried(server, dataset):
    dataset.fail_once(STATUS, status=503)

    client = build_client(server, max_retries=2)

    assert client.system_status().version == "5.30.0.9730"
    assert len(server.requests_for("/api/v3/system/status")) == 2


def test_a_broken_server_raises_an_unavailable_error(server, dataset):
    dataset.fail_always(STATUS, status=503)

    client = build_client(server, max_retries=1)

    with pytest.raises(RadarrUnavailableError) as failure:
        client.system_status()

    assert "/api/v3/system/status" in str(failure.value)
    assert len(server.requests_for("/api/v3/system/status")) == 2


def test_a_redirect_is_never_followed_so_the_key_cannot_leak(server, dataset):
    dataset.api_key = "super-secret-key"
    dataset.redirects[STATUS] = "http://127.0.0.1:9/steal-the-key"

    client = build_client(server, api_key="super-secret-key")

    with pytest.raises(RadarrError) as failure:
        client.system_status()

    assert not isinstance(failure.value, RadarrUnavailableError)
    assert "redirect" in str(failure.value)
    assert "super-secret-key" not in str(failure.value)
    # exactly one request: no retry and no follow
    assert len(server.requests_for("/api/v3/system/status")) == 1


def test_the_api_key_never_reaches_the_logs_or_an_error_message(server, dataset, caplog):
    dataset.api_key = "super-secret-key"
    dataset.fail_always(STATUS, status=503)
    client = build_client(server, api_key="super-secret-key", max_retries=0)

    with pytest.raises(RadarrUnavailableError) as failure:
        client.system_status()

    assert "super-secret-key" not in str(failure.value)
    assert "super-secret-key" not in repr(client)
    assert "super-secret-key" not in caplog.text


def test_delete_movie_sends_the_exact_delete_request(server, dataset):
    dataset.movies = [movie_resource(7, "Tenet"), movie_resource(8, "Blade Runner 2049")]
    client = build_client(server)

    client.delete_movie(7, delete_files=True)
    assert [entry["id"] for entry in dataset.movies] == [8]

    client.delete_movie(8, delete_files=False)
    assert dataset.movies == []

    deletes = [
        request
        for request in server.requests
        if request["method"] == "DELETE" and request["path"].startswith("/api/v3/movie/")
    ]
    assert [(request["path"], request["query"]) for request in deletes] == [
        ("/api/v3/movie/7", {"deleteFiles": "true", "addImportExclusion": "false"}),
        ("/api/v3/movie/8", {"deleteFiles": "false", "addImportExclusion": "false"}),
    ]


def test_unmonitor_movie_puts_the_whole_resource_with_monitored_false(server, dataset):
    dataset.movies = [movie_resource(7, "Tenet")]
    client = build_client(server)

    updated = client.unmonitor_movie(7)

    puts = [
        request
        for request in server.requests_for("/api/v3/movie/7")
        if request["method"] == "PUT"
    ]
    assert len(puts) == 1
    assert puts[0]["body"]["monitored"] is False
    assert puts[0]["body"]["title"] == "Tenet"
    assert updated.monitored is False
    assert dataset.movies[0]["monitored"] is False
    gets = [
        request
        for request in server.requests_for("/api/v3/movie/7")
        if request["method"] == "GET"
    ]
    assert len(gets) == 1


def test_add_import_exclusion_posts_the_tmdb_id_and_title(server, dataset):
    client = build_client(server)

    exclusion = client.add_import_exclusion(577922, "Tenet")

    posts = [
        request
        for request in server.requests_for("/api/v3/importlistexclusion")
        if request["method"] == "POST"
    ]
    assert len(posts) == 1
    assert posts[0]["body"] == {"tmdbId": 577922, "title": "Tenet"}
    assert exclusion.id == 1
    assert exclusion.tmdbId == 577922
    assert exclusion.title == "Tenet"
    assert dataset.import_exclusions[0]["tmdbId"] == 577922


def test_a_missing_movie_raises_a_structured_error(server):
    client = build_client(server)

    with pytest.raises(RadarrError) as failure:
        client.movie(999)

    assert not isinstance(failure.value, RadarrUnavailableError)
    assert "/api/v3/movie/999" in str(failure.value)


def test_from_settings_reads_the_radarr_connection(server):
    settings = SimpleNamespace(
        radarr_base_url=server.base_url + "/",
        radarr_api_key="test-api-key",
    )

    client = RadarrClient.from_settings(
        settings, rate_limit_per_second=1000.0, backoff_seconds=0.0
    )

    assert client.base_url == server.base_url
    assert client.system_status().version == "5.30.0.9730"
