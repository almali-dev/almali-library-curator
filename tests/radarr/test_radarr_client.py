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


def test_missing_movie_has_a_distinct_not_found_error(server):
    from almali_library_curator.radarr import RadarrNotFound

    with pytest.raises(RadarrNotFound):
        build_client(server).movie(999)


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


# -- the monitoring writes are monitoring-only (#106) -------------------------


def non_read_requests(server):
    return [
        request for request in server.requests if request["method"] != "GET"
    ]


def test_unmonitor_movie_writes_only_the_monitored_flag(server, dataset):
    """#106: the write names the flag, never the resource the read returned."""
    dataset.movies = [movie_resource(7, "Tenet")]
    client = build_client(server)

    updated = client.unmonitor_movie(7)

    writes = non_read_requests(server)
    assert [(request["method"], request["path"]) for request in writes] == [
        ("PUT", "/api/v3/movie/editor")
    ]
    assert writes[0]["body"] == {"movieIds": [7], "monitored": False}
    assert updated.movie.monitored is False
    assert updated.changed is True
    assert dataset.movies[0]["monitored"] is False
    # One read, one write: the write is not the movie resource itself.
    assert [
        request["method"] for request in server.requests_for("/api/v3/movie/7")
    ] == ["GET"]


def test_monitor_movie_writes_only_the_monitored_flag(server, dataset):
    """#106: the restore names the flag too, never the whole resource."""
    dataset.movies = [movie_resource(7, "Tenet", monitored=False)]
    client = build_client(server)

    updated = client.monitor_movie(7)

    writes = non_read_requests(server)
    assert [(request["method"], request["path"]) for request in writes] == [
        ("PUT", "/api/v3/movie/editor")
    ]
    assert writes[0]["body"] == {"movieIds": [7], "monitored": True}
    assert updated.movie.monitored is True
    assert updated.changed is True
    assert dataset.movies[0]["monitored"] is True


def test_a_movie_write_does_not_carry_a_field_another_client_changed(
    server, dataset
):
    """#106: the write sends what it names, never what it read.

    The other client's change lands between the restore's read and its
    write, which is exactly the window a whole-resource write loses.
    """
    dataset.movies = [movie_resource(7, "Tenet")]
    client = build_client(server)
    client.unmonitor_movie(7)
    server.requests.clear()
    dataset.on_write = lambda method, path: dataset.movies[0].update(
        {"qualityProfileId": 7}
    )

    client.monitor_movie(7)

    assert dataset.movies[0]["qualityProfileId"] == 7
    assert dataset.movies[0]["monitored"] is True
    assert [
        (request["method"], request["path"]) for request in non_read_requests(server)
    ] == [("PUT", "/api/v3/movie/editor")]


def test_an_unmonitor_of_an_unmonitored_movie_changes_nothing(server, dataset):
    """#103: the removal must know it did not unmonitor this movie."""
    dataset.movies = [movie_resource(7, "Tenet", monitored=False)]
    client = build_client(server)

    change = client.unmonitor_movie(7)

    assert change.changed is False
    assert change.movie.monitored is False
    assert [request["method"] for request in server.requests] == ["GET"]


def test_add_import_exclusion_posts_the_real_radarr_payload(server, dataset):
    client = build_client(server)

    exclusion = client.add_import_exclusion(577922, "Tenet", 2020)

    posts = [
        request
        for request in server.requests_for("/api/v3/exclusions")
        if request["method"] == "POST"
    ]
    assert len(posts) == 1
    assert posts[0]["body"] == {
        "tmdbId": 577922,
        "movieTitle": "Tenet",
        "movieYear": 2020,
    }
    # The Sonarr-only path is not a Radarr endpoint.
    assert server.requests_for("/api/v3/importlistexclusion") == []
    assert exclusion.exclusion.id == 1
    assert exclusion.exclusion.tmdbId == 577922
    assert exclusion.exclusion.movieTitle == "Tenet"
    assert exclusion.exclusion.movieYear == 2020
    assert exclusion.created is True
    assert dataset.import_exclusions[0]["tmdbId"] == 577922


def test_an_existing_import_exclusion_is_not_posted_again(server, dataset):
    dataset.import_exclusions = [
        {"id": 4, "tmdbId": 577922, "movieTitle": "Tenet", "movieYear": 2020}
    ]
    client = build_client(server)

    exclusion = client.add_import_exclusion(577922, "Tenet", 2020)

    posts = [
        request
        for request in server.requests_for("/api/v3/exclusions")
        if request["method"] == "POST"
    ]
    assert posts == []
    assert len(dataset.import_exclusions) == 1
    assert exclusion.exclusion.id == 4
    assert exclusion.exclusion.movieTitle == "Tenet"
    # A pre-existing exclusion is not the app's to delete on a cancel (#103).
    assert exclusion.created is False


def test_lists_import_exclusions_from_the_real_radarr_endpoint(server, dataset):
    dataset.import_exclusions = [
        {"id": 4, "tmdbId": 577922, "movieTitle": "Tenet", "movieYear": 2020}
    ]

    entries = build_client(server).import_exclusions()

    assert [(entry.id, entry.tmdbId, entry.movieTitle) for entry in entries] == [
        (4, 577922, "Tenet")
    ]


# -- the reads the removal lifecycle makes before a write (#107) -------------


def test_movie_monitored_reads_the_flag_without_writing(server, dataset):
    dataset.movies = [
        movie_resource(7, "Tenet"),
        movie_resource(8, "Blade Runner 2049", monitored=False),
    ]
    client = build_client(server)

    assert client.movie_monitored(7) is True
    assert client.movie_monitored(8) is False
    assert [
        (request["method"], request["path"]) for request in server.requests
    ] == [("GET", "/api/v3/movie/7"), ("GET", "/api/v3/movie/8")]


def test_find_import_exclusion_reads_the_list_by_external_id(server, dataset):
    dataset.import_exclusions = [
        {"id": 4, "tmdbId": 577922, "movieTitle": "Tenet", "movieYear": 2020}
    ]
    client = build_client(server)

    found = client.find_import_exclusion(577922)

    assert found is not None and found.id == 4
    assert client.find_import_exclusion(1) is None
    assert all(request["method"] == "GET" for request in server.requests)


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


def test_the_request_journal_keeps_only_the_most_recent_requests(server, dataset):
    # #101: a long-lived client must not grow its journal without bound.
    dataset.movies = [movie_resource(7, "Tenet", monitored=True)]
    client = build_client(server, journal_size=2)

    client.system_status()
    client.system_status()
    client.all_movies()
    client.movie(7)

    assert [entry["path"] for entry in client.requests] == [
        "/api/v3/movie",
        "/api/v3/movie/7",
    ]


def test_the_request_journal_is_bounded_by_default(server):
    client = build_client(server)

    for _ in range(300):
        client.system_status()

    assert len(client.requests) == 256
    assert client.requests[-1]["path"] == "/api/v3/system/status"
