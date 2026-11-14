from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from almali_library_curator.sonarr import (
    SonarrAuthError,
    SonarrClient,
    SonarrError,
    SonarrUnavailableError,
)

STATUS = "GET /api/v3/system/status"


@pytest.fixture()
def dataset() -> FakeSonarrDataset:
    return FakeSonarrDataset()


@pytest.fixture()
def server(dataset):
    with FakeSonarrServer(dataset) as running:
        yield running


def build_client(server, **overrides) -> SonarrClient:
    options = {
        "base_url": server.base_url,
        "api_key": "test-api-key",
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return SonarrClient(**options)


def test_missing_series_has_a_distinct_not_found_error(server):
    from almali_library_curator.sonarr import SonarrNotFound

    with pytest.raises(SonarrNotFound):
        build_client(server).series(999)


def test_unmonitor_and_delete_only_the_selected_seasons(server, dataset):
    dataset.series = [series_resource(42, "Foundation")]
    dataset.episode_files = [
        {"id": 11, "seriesId": 42, "seasonNumber": 1},
        {"id": 12, "seriesId": 42, "seasonNumber": 2},
        {"id": 13, "seriesId": 77, "seasonNumber": 1},
    ]
    client = build_client(server)
    client.unmonitor_seasons(42, (1,))
    files = client.episode_files(42)
    client.delete_episode_files([entry.id for entry in files if entry.seasonNumber == 1])
    series = dataset.series[0]
    assert series["monitored"] is True
    assert [s["monitored"] for s in series["seasons"]] == [False, True]
    assert series["qualityProfileId"] == 1
    assert [entry["id"] for entry in dataset.episode_files] == [12, 13]
    request = server.requests_for("/api/v3/episodefile/bulk")[0]
    assert request["method"] == "DELETE"
    assert request["body"] == {"episodeFileIds": [11]}
    assert dataset.import_exclusions == []


def test_sends_the_api_key_in_the_header_and_never_in_the_url(server):
    client = build_client(server)

    client.system_status()

    request = server.requests_for("/api/v3/system/status")[0]
    assert request["headers"]["X-Api-Key"] == "test-api-key"
    assert "test-api-key" not in request["path"]
    assert "test-api-key" not in request["query"]


def test_system_status_reports_the_app_version(server):
    status = build_client(server).system_status()

    assert status.appName == "Sonarr"
    assert status.version == "3.0.9.1549"


def test_fetches_one_series_by_id(server, dataset):
    dataset.series = [series_resource(42, "Foundation", status="continuing", ended=False)]

    entry = build_client(server).series(42)

    assert entry.id == 42
    assert entry.title == "Foundation"
    assert entry.status == "continuing"
    assert entry.ended is False
    assert entry.monitored is True
    assert entry.tvdbId == 1042
    assert entry.seasonFolder is True
    assert entry.seasons[0].statistics.sizeOnDisk == 42 * 1024**3


def test_series_by_title_matches_case_insensitively(server, dataset):
    dataset.series = [
        series_resource(1, "The Office (US)"),
        series_resource(2, "Foundation"),
    ]

    client = build_client(server)

    assert client.series_by_title("foundation").id == 2
    assert client.series_by_title("THE OFFICE (US)").id == 1
    assert client.series_by_title("Missing") is None


def test_lookup_series_sends_the_term_and_returns_matches(server, dataset):
    dataset.series = [series_resource(1, "Foundation"), series_resource(2, "Lost in Space")]

    found = build_client(server).lookup_series("found")

    assert [entry.title for entry in found] == ["Foundation"]
    request = server.requests_for("/api/v3/series/lookup")[0]
    assert request["query"] == {"term": "found"}


def test_a_wrong_api_key_raises_an_auth_error(server):
    client = build_client(server, api_key="wrong-key")

    with pytest.raises(SonarrAuthError):
        client.system_status()


def test_a_transient_failure_is_retried(server, dataset):
    dataset.fail_once(STATUS, status=503)

    client = build_client(server, max_retries=2)

    assert client.system_status().version == "3.0.9.1549"
    assert len(server.requests_for("/api/v3/system/status")) == 2


def test_a_broken_server_raises_an_unavailable_error(server, dataset):
    dataset.fail_always(STATUS, status=503)

    client = build_client(server, max_retries=1)

    with pytest.raises(SonarrUnavailableError) as failure:
        client.system_status()

    assert "/api/v3/system/status" in str(failure.value)
    assert len(server.requests_for("/api/v3/system/status")) == 2


def test_a_redirect_is_never_followed_so_the_key_cannot_leak(server, dataset):
    dataset.api_key = "super-secret-key"
    dataset.redirects[STATUS] = "http://127.0.0.1:9/steal-the-key"

    client = build_client(server, api_key="super-secret-key")

    with pytest.raises(SonarrError) as failure:
        client.system_status()

    assert not isinstance(failure.value, SonarrUnavailableError)
    assert "redirect" in str(failure.value)
    assert "super-secret-key" not in str(failure.value)
    # exactly one request: no retry and no follow
    assert len(server.requests_for("/api/v3/system/status")) == 1


def test_the_api_key_never_reaches_the_logs_or_an_error_message(server, dataset, caplog):
    dataset.api_key = "super-secret-key"
    dataset.fail_always(STATUS, status=503)
    client = build_client(server, api_key="super-secret-key", max_retries=0)

    with pytest.raises(SonarrUnavailableError) as failure:
        client.system_status()

    assert "super-secret-key" not in str(failure.value)
    assert "super-secret-key" not in repr(client)
    assert "super-secret-key" not in caplog.text


def test_delete_series_sends_the_exact_delete_request(server, dataset):
    dataset.series = [series_resource(42, "Lost in Space"), series_resource(43, "Foundation")]
    client = build_client(server)

    client.delete_series(42, delete_files=True)
    assert [entry["id"] for entry in dataset.series] == [43]

    client.delete_series(43, delete_files=False)
    assert dataset.series == []

    deletes = [
        request
        for request in server.requests
        if request["method"] == "DELETE" and request["path"].startswith("/api/v3/series/")
    ]
    assert [(request["path"], request["query"]) for request in deletes] == [
        ("/api/v3/series/42", {"deleteFiles": "true"}),
        ("/api/v3/series/43", {"deleteFiles": "false"}),
    ]


def test_unmonitor_series_puts_the_whole_resource_with_monitored_false(server, dataset):
    dataset.series = [series_resource(42, "Foundation")]
    client = build_client(server)

    updated = client.unmonitor_series(42)

    puts = [
        request
        for request in server.requests_for("/api/v3/series/42")
        if request["method"] == "PUT"
    ]
    assert len(puts) == 1
    assert puts[0]["body"]["monitored"] is False
    assert puts[0]["body"]["title"] == "Foundation"
    assert updated.monitored is False
    assert dataset.series[0]["monitored"] is False
    gets = [
        request
        for request in server.requests_for("/api/v3/series/42")
        if request["method"] == "GET"
    ]
    assert len(gets) == 1


def test_add_import_exclusion_posts_the_tvdb_id_and_title(server, dataset):
    client = build_client(server)

    exclusion = client.add_import_exclusion(735888, "Lost in Space")

    posts = [
        request
        for request in server.requests_for("/api/v3/importlistexclusion")
        if request["method"] == "POST"
    ]
    assert len(posts) == 1
    assert posts[0]["body"] == {"tvdbId": 735888, "title": "Lost in Space"}
    assert exclusion.id == 1
    assert exclusion.tvdbId == 735888
    assert exclusion.title == "Lost in Space"
    assert dataset.import_exclusions[0]["tvdbId"] == 735888


def test_an_existing_import_exclusion_is_not_posted_again(server, dataset):
    dataset.import_exclusions = [
        {"id": 4, "tvdbId": 735888, "title": "Lost in Space"}
    ]
    client = build_client(server)

    exclusion = client.add_import_exclusion(735888, "Lost in Space")

    posts = [
        request
        for request in server.requests_for("/api/v3/importlistexclusion")
        if request["method"] == "POST"
    ]
    assert posts == []
    assert len(dataset.import_exclusions) == 1
    assert exclusion.id == 4
    assert exclusion.title == "Lost in Space"


def test_lists_import_exclusions_from_the_real_sonarr_endpoint(server, dataset):
    dataset.import_exclusions = [
        {"id": 4, "tvdbId": 735888, "title": "Lost in Space"}
    ]

    entries = build_client(server).import_exclusions()

    assert [(entry.id, entry.tvdbId, entry.title) for entry in entries] == [
        (4, 735888, "Lost in Space")
    ]


def test_a_missing_series_raises_a_structured_error(server):
    client = build_client(server)

    with pytest.raises(SonarrError) as failure:
        client.series(999)

    assert not isinstance(failure.value, SonarrUnavailableError)
    assert "/api/v3/series/999" in str(failure.value)


def test_from_settings_reads_the_sonarr_connection(server):
    settings = SimpleNamespace(
        sonarr_base_url=server.base_url + "/",
        sonarr_api_key="test-api-key",
    )

    client = SonarrClient.from_settings(
        settings, rate_limit_per_second=1000.0, backoff_seconds=0.0
    )

    assert client.base_url == server.base_url
    assert client.system_status().version == "3.0.9.1549"
