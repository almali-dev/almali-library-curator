"""The Plex collection write operations (S5 step 3).

Reads stay exactly as they were; the new writes go through the same
transport (token header, redirect refusal, retries) and the same fake
server. Live-server payload fidelity is the recorded T15 caveat (L22).
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie

from almali_library_curator.plex import PlexClient, PlexError, PlexUnavailableError


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


def test_ensure_collection_creates_only_when_missing(server, dataset):
    dataset.items["1"] = [movie("11", "Heat")]
    client = build_client(server)

    first = client.ensure_collection("1", "Leaving soon")
    second = client.ensure_collection("1", "Leaving soon")

    assert first == second
    creates = [
        request
        for request in server.requests_for("/library/sections/1/collections")
        if request["method"] == "POST"
    ]
    assert len(creates) == 1
    assert creates[0]["body"] == {"title": "Leaving soon"}
    assert [entry.title for entry in client.collections("1")] == ["Leaving soon"]


def test_ensure_collection_returns_an_existing_collection_without_creating(
    server, dataset
):
    dataset.collections["1"] = [
        {"ratingKey": "77", "key": "/library/collections/77", "type": "collection", "title": "Leaving soon"}
    ]
    dataset.collection_items["77"] = set()
    client = build_client(server)

    key = client.ensure_collection("1", "Leaving soon")

    assert key == "77"
    posts = [
        request
        for request in server.requests_for("/library/sections/1/collections")
        if request["method"] == "POST"
    ]
    assert posts == []


def test_add_to_collection_puts_the_item_in_the_collection(server, dataset):
    dataset.items["1"] = [movie("11", "Heat")]
    client = build_client(server)
    key = client.ensure_collection("1", "Leaving soon")

    client.add_to_collection(key, "11")

    assert dataset.collection_items[key] == {"11"}
    request = server.requests_for(f"/library/collections/{key}/items/11")[0]
    assert request["method"] == "PUT"


def test_remove_from_collection_takes_the_item_out(server, dataset):
    dataset.items["1"] = [movie("11", "Heat")]
    client = build_client(server)
    key = client.ensure_collection("1", "Leaving soon")
    client.add_to_collection(key, "11")

    client.remove_from_collection(key, "11")

    assert dataset.collection_items[key] == set()
    request = server.requests_for(f"/library/collections/{key}/items/11")[-1]
    assert request["method"] == "DELETE"


def test_writing_to_an_unknown_collection_is_an_error(server, dataset):
    client = build_client(server)

    with pytest.raises(PlexError):
        client.add_to_collection("999", "11")


def test_a_failing_write_is_reported(server, dataset):
    dataset.collections["1"] = [
        {"ratingKey": "911", "key": "/library/collections/911", "type": "collection", "title": "Leaving soon"}
    ]
    dataset.collection_items["911"] = set()
    dataset.fail_always("/library/collections/911/items/11", status=503)
    client = build_client(server, max_retries=0)

    with pytest.raises(PlexUnavailableError):
        client.add_to_collection("911", "11")


def test_the_token_travels_in_the_header_on_writes_too(server, dataset):
    dataset.items["1"] = [movie("11", "Heat")]
    client = build_client(server, token="super-secret-token")
    key = client.ensure_collection("1", "Leaving soon")
    client.add_to_collection(key, "11")

    for path in (
        "/library/sections/1/collections",
        f"/library/collections/{key}/items/11",
    ):
        for request in server.requests_for(path):
            assert request["headers"]["X-Plex-Token"] == "super-secret-token"
            assert "super-secret-token" not in request["query"]
