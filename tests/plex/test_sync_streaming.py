"""The sync converts, stores and drops one page at a time (#60).

A full library used to be materialised twice: the client accumulated every
raw entry and then built a model list, and the sync kept the pending rows of
every table until the publication opened. Pages now arrive one at a time and
are stored as they arrive, so the memory a sync needs follows one page
instead of the size of the library.

The snapshot is still published in one transaction, so a read that fails on
a later page publishes nothing and keeps the caller's pending work.
"""

from __future__ import annotations

import weakref

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    history_entry,
    movie,
)
from sync_snapshot import snapshot

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexError, PlexSync
from almali_library_curator.plex.models import PlexMetadata

PAGE_SIZE = 3


def build_client(server, **overrides) -> PlexClient:
    options = {
        "base_url": server.base_url,
        "token": "test-token",
        "page_size": PAGE_SIZE,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
        "max_retries": 0,
    }
    options.update(overrides)
    return PlexClient(**options)


def watched_movies(count: int) -> FakePlexDataset:
    """``count`` movies in one section and one play event each."""
    data = FakePlexDataset()
    data.items["1"] = [
        movie(str(100 + index), f"Movie {index}") for index in range(count)
    ]
    data.history = [
        history_entry(str(200 + index), 1, 1_700_000_000 + index, 6_000_000, 6_000_000)
        for index in range(count)
    ]
    return data


class Enough(Exception):
    """Stops a page stream from the consumer side."""


def test_a_consumer_that_stops_after_the_first_page_asks_for_one_page():
    """The pages are handed over as they arrive; a short read stays short."""
    data = watched_movies(8)
    with FakePlexServer(data) as server:
        client = build_client(server)
        page_keys: list[list[str]] = []

        def stop_after_the_first_page(page: list[PlexMetadata]) -> None:
            page_keys.append([item.ratingKey for item in page])
            raise Enough

        with pytest.raises(Enough):
            client.section_items("1", "movie", on_page=stop_after_the_first_page)

        assert page_keys == [["100", "101", "102"]]
        assert len(server.requests_for("/library/sections/1/all")) == 1


def test_the_sync_holds_one_page_of_item_models_at_a_time(tmp_path, monkeypatch):
    """Peak memory proxy: the models of a page are released as it is stored."""
    data = watched_movies(3 * PAGE_SIZE)
    live = 0
    peak = 0
    validated = PlexMetadata.model_validate.__func__

    def released() -> None:
        nonlocal live
        live -= 1

    def counting(cls, entry, *args, **kwargs):
        nonlocal live, peak
        model = validated(cls, entry, *args, **kwargs)
        live += 1
        peak = max(peak, live)
        weakref.finalize(model, released)
        return model

    monkeypatch.setattr(PlexMetadata, "model_validate", classmethod(counting))
    conn = init_db(tmp_path / "almali-library-curator.db")
    try:
        with FakePlexServer(data) as server:
            summary = PlexSync(build_client(server), conn).run()
    finally:
        conn.close()

    assert summary.media_items == 3 * PAGE_SIZE
    # One page is being stored while the next one is handed over, and no
    # page outlives the one after it.
    assert peak <= 2 * PAGE_SIZE


class FailsOnTheThirdPageServer(FakePlexServer):
    """A server that refuses a read once two pages were handed over."""

    def _failure_status(self, path: str, query: dict) -> int | None:
        if path == "/library/sections/1/all" and len(self.requests_for(path)) >= 3:
            return 503
        return super()._failure_status(path, query)


def test_a_failure_after_two_pages_publishes_nothing_and_keeps_the_callers_work(
    tmp_path,
):
    data = watched_movies(3 * PAGE_SIZE)
    conn = init_db(tmp_path / "almali-library-curator.db")
    with FakePlexServer(data) as server:
        PlexSync(build_client(server), conn).run()
    # The caller's own transaction and its pending row, which the sync joins.
    conn.execute(
        "INSERT INTO libraries (plex_key, title, section_type)"
        " VALUES ('900', 'Pending by the caller', 'movie')"
    )
    assert conn.in_transaction
    before = snapshot(conn)
    # A published partial read would hide this item: it is no longer listed.
    data.items["1"] = data.items["1"][:-1]
    with FailsOnTheThirdPageServer(data) as failing:
        with pytest.raises(PlexError):
            PlexSync(build_client(failing), conn).run()
        # Two pages were read and stored before the third request refused.
        assert len(failing.requests_for("/library/sections/1/all")) == 3

    assert snapshot(conn) == before
    # The caller's transaction is still open and still owns its own work.
    assert conn.in_transaction
    conn.commit()
    conn.close()
