"""The paged reads of the Plex client (issue #51).

A live server may answer a paged collection without ``totalSize``. The
client then reads until a page comes back short instead of stopping after
the first page, and it still stops at ``totalSize`` when the server sends it.
"""

from __future__ import annotations

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    history_entry,
    movie,
)

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexError, PlexSync


class RepeatingPageServer(FakePlexServer):
    """A server that ignores the offset and answers the first page forever.

    It also omits ``totalSize``, so the short-page rule is the only thing
    that could end the read.
    """

    def _paged(self, items: list[dict], query: dict, path: str = "") -> dict:
        payload = super()._paged(items, query, path)
        requested = int(query.get("X-Plex-Container-Size", len(items) or 1))
        payload["MediaContainer"]["Metadata"] = items[:requested]
        payload["MediaContainer"]["offset"] = 0
        return payload


class MislabelledTotalServer(FakePlexServer):
    """A server that sends a ``totalSize`` that is not a number."""

    def _paged(self, items: list[dict], query: dict, path: str = "") -> dict:
        payload = super()._paged(items, query, path)
        if path == "/status/sessions/history/all":
            payload["MediaContainer"]["totalSize"] = "many"
        return payload


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
        "page_size": 2,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return PlexClient(**options)


def watched(count: int) -> list[dict]:
    return [
        history_entry(str(100 + index), 1, 1_700_000_000 + index, 6_000_000, 6_000_000)
        for index in range(count)
    ]


def offsets(server) -> list[int]:
    return [
        int(request["query"]["X-Plex-Container-Start"])
        for request in server.requests_for("/status/sessions/history/all")
    ]


def test_history_reads_until_a_short_page_when_total_size_is_absent(server, dataset):
    dataset.omit_total_size = True
    dataset.history = watched(5)

    entries = build_client(server).history()

    assert [entry.ratingKey for entry in entries] == [str(100 + i) for i in range(5)]
    assert offsets(server) == [0, 2, 4]
    assert [request["query"]["X-Plex-Container-Size"] for request in server.requests_for(
        "/status/sessions/history/all"
    )] == ["2", "2", "2"]


def test_history_stops_at_total_size_without_an_extra_probe(server, dataset):
    dataset.history = watched(4)

    entries = build_client(server).history()

    assert [entry.ratingKey for entry in entries] == [str(100 + i) for i in range(4)]
    # The two full pages already reach the reported total, so the read ends
    # there instead of asking for a third, empty page.
    assert offsets(server) == [0, 2]


def test_collections_read_until_a_short_page_when_total_size_is_absent(server, dataset):
    dataset.omit_total_size = True
    dataset.collections["1"] = [
        dataset.create_collection("1", f"Collection {index}") for index in range(3)
    ]

    collections = build_client(server).collections("1")

    assert [entry.title for entry in collections] == [
        "Collection 0",
        "Collection 1",
        "Collection 2",
    ]
    assert [
        int(request["query"]["X-Plex-Container-Start"])
        for request in server.requests_for("/library/sections/1/collections")
    ] == [0, 2]


def test_a_full_page_that_repeats_refuses_instead_of_looping(server, dataset):
    dataset.history = watched(4)
    dataset.omit_total_size = True
    with RepeatingPageServer(dataset) as repeating:
        client = build_client(repeating)

        with pytest.raises(PlexError):
            client.history()

        assert len(repeating.requests_for("/status/sessions/history/all")) == 2


def test_a_section_read_without_total_size_still_refuses(server, dataset):
    dataset.omit_total_size = True
    dataset.items["1"] = [movie("100", "Tenet")]

    with pytest.raises(PlexError):
        build_client(server).section_items("1", "movie")

    assert len(server.requests_for("/library/sections/1/all")) == 1


def test_a_total_size_that_is_not_a_number_still_refuses(server, dataset):
    dataset.history = watched(2)
    with MislabelledTotalServer(dataset) as mislabelled:
        with pytest.raises(PlexError):
            build_client(mislabelled).history()

        assert len(mislabelled.requests_for("/status/sessions/history/all")) == 1


def test_a_play_on_a_later_page_is_still_synced(server, dataset, tmp_path):
    dataset.omit_total_size = {"/status/sessions/history/all"}
    dataset.items["1"] = [movie("100", "Tenet")]
    dataset.history = watched(3)
    conn = init_db(tmp_path / "almali-library-curator.db")
    try:
        summary = PlexSync(build_client(server), conn).run()

        assert summary.play_events == 3
        stored = conn.execute(
            "SELECT plex_rating_key FROM play_events ORDER BY plex_rating_key"
        ).fetchall()
        assert [row["plex_rating_key"] for row in stored] == ["100", "101", "102"]
    finally:
        conn.close()
