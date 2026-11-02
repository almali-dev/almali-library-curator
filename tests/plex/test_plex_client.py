from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from library_curator.plex import PlexAuthError, PlexClient, PlexError, PlexUnavailableError


@pytest.fixture()
def dataset() -> FakePlexDataset:
    return FakePlexDataset()


@pytest.fixture()
def server(dataset):
    with FakePlexServer(dataset) as running:
        yield running


def build_client(server, **overrides) -> PlexClient:
    options = {
        "base_url": server.base_url,
        "token": "test-token",
        "page_size": 100,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return PlexClient(**options)


def test_reads_a_library_in_pages(server, dataset):
    dataset.items["1"] = [movie(str(index), f"Movie {index}") for index in range(450)]

    client = build_client(server, page_size=200)
    items = client.section_items("1", "movie")

    assert len(items) == 450
    assert len(server.requests_for("/library/sections/1/all")) == 3


def test_sends_the_token_in_the_header_and_never_in_the_url(server):
    client = build_client(server)

    client.sections()

    request = server.requests_for("/library/sections")[0]
    assert request["headers"]["X-Plex-Token"] == "test-token"
    assert "test-token" not in request["query"]


def test_maps_plex_paths_to_local_paths(server, dataset):
    dataset.items["1"] = [movie("11", "Heat")]
    client = build_client(server, path_map=(("/media", "/mnt/user/media"),))

    items = client.section_items("1", "movie")

    assert items[0].Part[0].file == "/media/Movies/Heat/Heat.mkv"
    assert client.map_path("/media/Movies/Heat/Heat.mkv") == "/mnt/user/media/Movies/Heat/Heat.mkv"


def test_a_transient_failure_is_retried(server, dataset):
    dataset.fail_once("/library/sections", status=503)

    client = build_client(server, max_retries=2)

    sections = client.sections()

    assert [section.title for section in sections] == ["Movies", "Shows"]
    assert len(server.requests_for("/library/sections")) == 2


def test_a_broken_server_raises_a_structured_error(server, dataset):
    dataset.fail_always("/library/sections", status=503)

    client = build_client(server, max_retries=1)

    with pytest.raises(PlexUnavailableError) as failure:
        client.sections()

    assert "/library/sections" in str(failure.value)


def test_rejected_credentials_raise_an_auth_error(server, dataset):
    dataset.fail_always("/library/sections", status=401)

    client = build_client(server)

    with pytest.raises(PlexAuthError):
        client.sections()


def test_the_token_never_reaches_the_logs_or_an_error_message(server, dataset, caplog):
    dataset.fail_always("/library/sections", status=503)
    client = build_client(server, token="super-secret-token", max_retries=0)

    with pytest.raises(PlexUnavailableError) as failure:
        client.sections()

    assert "super-secret-token" not in str(failure.value)
    assert "super-secret-token" not in caplog.text


def test_a_redirect_is_never_followed_so_the_token_cannot_leak(server, dataset):
    dataset.redirects["/library/sections"] = "http://127.0.0.1:9/steal-the-token"

    client = build_client(server, token="super-secret-token")

    with pytest.raises(PlexError) as failure:
        client.sections()

    assert not isinstance(failure.value, PlexUnavailableError)
    assert "redirect" in str(failure.value)
    assert "super-secret-token" not in str(failure.value)
    assert len(server.requests_for("/library/sections")) == 1
