"""#144: the request-path collection client, against a real Plex fake.

S1 of the issue: a Mark or an Undo makes one bounded attempt per Plex
collection call, the process-wide collection lock is never waited on longer
than that, and a write that still fails leaves the durable request for the
``retry_collection_additions`` the scan already runs. S3: the client the scan
and the executor build keeps its retries, so the bound is the web layer's
choice and never a new default.
"""

from __future__ import annotations

import threading
import time

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer

from lifecycle_helpers import FrozenClock, default_settings, make_candidate, seed_item, seed_library

from almali_library_curator.lifecycle import LifecycleEngine, RemovalState
from almali_library_curator.plex import PlexClient
from almali_library_curator.plex.client import PlexUnavailableError

TOKEN = "fail-fast-collection-token-1a09"
# The collection the fake Plex creates for the default collection name.
COLLECTION = "900001"


def request_path_client(server, *, lock_timeout: float | None = 5.0) -> PlexClient:
    """The client the web layer builds for Mark and Undo (S1)."""
    return PlexClient(server.base_url, TOKEN, timeout=5.0, rate_limit_per_second=0,
                      max_retries=0, backoff_seconds=0, collection_lock_timeout=lock_timeout)


@pytest.fixture()
def movie(conn):
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    return make_candidate(item, "11", "Tenet")


def test_one_bounded_attempt_and_the_retry_completes_the_request(conn, movie):
    """The failure is one call, and the durable request is finished later (S1)."""
    data = FakePlexDataset()
    data.fail_always("/library/sections/1/collections", 503)
    with FakePlexServer(data) as server:
        engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                                 collections=request_path_client(server))

        with pytest.raises(PlexUnavailableError):
            engine.mark_for_removal(movie)

        # One attempt, never a retry ladder: the click owes Plex one call.
        assert len(server.requests_for("/library/sections/1/collections")) == 1
        request = conn.execute("SELECT * FROM removal_requests").fetchone()
        assert request["state"] == RemovalState.MARKED.value
        assert request["collection_added"] == 0
        assert data.collection_items == {}

        # The retry the scan already runs catches up, as it does today.
        data.always_fail.clear()
        assert engine.retry_collection_additions() == 1
        assert data.collection_items == {COLLECTION: {"11"}}
        assert conn.execute("SELECT collection_added FROM removal_requests").fetchone()[0] == 1


def test_the_collection_lock_is_never_waited_on_past_its_bound():
    """A holder that hangs costs a request one bounded wait, then a failure (S1)."""
    from almali_library_curator.plex.client import _COLLECTION_LOCK

    data = FakePlexDataset()
    with FakePlexServer(data) as server:
        client = request_path_client(server, lock_timeout=0.05)
        assert _COLLECTION_LOCK.acquire(timeout=1)
        try:
            started = time.monotonic()
            with pytest.raises(PlexUnavailableError):
                client.ensure_collection("1", "Leaving soon")
            elapsed = time.monotonic() - started
        finally:
            _COLLECTION_LOCK.release()

        assert elapsed < 1.0, elapsed
        # Nothing reached Plex: the caller gave up instead of queueing.
        assert server.requests == []


def test_the_default_client_still_waits_for_the_lock():
    """Scans and the executor keep queueing: the bound is the web's only (S3)."""
    from almali_library_curator.plex.client import _COLLECTION_LOCK

    data = FakePlexDataset()
    with FakePlexServer(data) as server:
        # The production client: no lock timeout, the retries the scans rely on.
        client = PlexClient.from_settings(default_settings(plex_base_url=server.base_url,
                                                           plex_token=TOKEN,
                                                           plex_rate_limit_per_second=0))
        assert client.collection_lock_timeout is None
        assert client.max_retries == 3
        assert _COLLECTION_LOCK.acquire(timeout=1)
        threading.Timer(0.1, _COLLECTION_LOCK.release).start()

        # It waited for the holder instead of failing.
        assert client.ensure_collection("1", "Leaving soon") == COLLECTION
        assert data.collection_items == {COLLECTION: set()}
