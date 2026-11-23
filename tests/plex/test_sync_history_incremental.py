"""#74: a later sync asks Plex only for the history it does not have yet.

The history endpoint is read newest first and the read ends on the first
page that carries nothing newer than the stored cursor. The order is only
trusted once the server has proven it across a page boundary, so a server
that ignores the requested sort is still read in full rather than cut
short: an under-read history would make a watched item look never watched.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, history_entry, movie
from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync, PlexUnavailableError

HISTORY_PATH = "/status/sessions/history/all"
IDENTITY = "fake-plex-machine-identifier"
CURSOR_KEY = f"plex_history_viewed_at:{IDENTITY}"
NEWEST = 1_700_000_000


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet")]
    data.accounts = [{"id": 1, "name": "Marc"}]
    return data


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "curator.db")
    yield connection
    connection.close()


def client_for(server, page_size: int = 2) -> PlexClient:
    return PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=page_size,
        rate_limit_per_second=0,
        backoff_seconds=0.0,
    )


def twenty_events() -> list[dict]:
    """Twenty plays, newest first, at two per page.

    The minutes go by tens, so no two events share an instant and the
    stored cursor is unambiguous.
    """
    return [
        history_entry("100", 1, NEWEST - 60 * number, 6_000_000, 6_000_000)
        for number in range(20)
    ]


def cursor(conn, key: str = CURSOR_KEY) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def test_a_second_sync_reads_only_the_pages_that_may_hold_new_history(dataset, conn):
    dataset.history = twenty_events()
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 10  # ten pages of two
        assert cursor(conn) == str(NEWEST)

        # Nothing changed: the read proves the newest-first order on its
        # second page and stops there instead of walking all ten.
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 12

        dataset.history.insert(
            0, history_entry("100", 1, NEWEST + 60, 6_000_000, 6_000_000)
        )
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 14

    rows = conn.execute(
        "SELECT played_at, source FROM play_events ORDER BY played_at DESC"
    ).fetchall()
    assert len(rows) == 21
    assert tuple(rows[0]) == ("2023-11-14T22:14:20+00:00", "plex")


def test_a_server_that_ignores_the_requested_order_is_still_read_in_full(dataset, conn):
    """An unordered answer never proves that nothing newer follows."""
    dataset.history_ignores_sort = True
    # Stored oldest first, which is what a server that ignores the requested
    # newest-first order answers.
    dataset.history = list(reversed(twenty_events()))
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 10
        assert cursor(conn) == str(NEWEST)

        PlexSync(client_for(server), conn).run()
        # No early stop: the server did not prove the order it was asked for.
        assert len(server.requests_for(HISTORY_PATH)) == 20

    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 20


def test_a_failed_history_read_leaves_the_cursor_where_it_was(dataset, conn):
    dataset.history = [history_entry("100", 1, NEWEST, 6_000_000, 6_000_000)]
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert cursor(conn) == str(NEWEST)

        dataset.always_fail[HISTORY_PATH] = 503
        with pytest.raises(PlexUnavailableError):
            PlexSync(client_for(server), conn).run()
    assert cursor(conn) == str(NEWEST)


def test_a_first_sync_asks_for_no_window_and_stores_every_event(dataset, conn):
    """An empty cursor means the whole history, exactly as before #74."""
    dataset.history = twenty_events()
    with FakePlexServer(dataset) as server:
        summary = PlexSync(client_for(server), conn).run()
        first = server.requests_for(HISTORY_PATH)[0]
    assert "sort" not in first["query"]
    assert summary.play_events == 20


def test_another_plex_server_is_read_in_full(dataset, conn):
    """A cursor of one server proves nothing about the history of another.

    A server replaced at the same address can hold plays older than the
    cursor the previous one left; skipping them would hide real watches.
    """
    dataset.history = twenty_events()
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 10

        dataset.identity["machineIdentifier"] = "replaced-server"
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 20
    assert cursor(conn) == str(NEWEST)
    assert cursor(conn, "plex_history_viewed_at:replaced-server") == str(NEWEST)


def test_a_server_that_cannot_name_itself_is_read_in_full(dataset, conn):
    """No identity, no cursor: trust nothing instead of someone else's key."""
    dataset.history = twenty_events()
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert cursor(conn) == str(NEWEST)

        dataset.always_fail["/"] = 500
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 20


def test_a_history_entry_without_a_rating_key_is_skipped_and_still_moves_the_cursor(dataset, conn):
    """#118: Plex keeps the plays of a removed item without linking any item.

    Such an entry cannot match a candidate, so it is skipped instead of
    failing the whole sync, and its instant still moves the cursor so the
    next read does not ask for it again.
    """
    orphan = history_entry("unused", 1, NEWEST, 6_000_000, 6_000_000)
    del orphan["ratingKey"]
    del orphan["key"]
    dataset.history = [
        orphan,
        history_entry("100", 1, NEWEST - 60, 6_000_000, 6_000_000),
    ]
    with FakePlexServer(dataset) as server:
        summary = PlexSync(client_for(server), conn).run()

    assert summary.play_events == 1
    rows = conn.execute("SELECT plex_rating_key FROM play_events").fetchall()
    assert [tuple(row) for row in rows] == [("100",)]
    assert cursor(conn) == str(NEWEST)
