"""#74: a sync of unchanged rows writes far less than it used to.

Every upsert updated every column, so a second scan of an unchanged
library rewrote 200,000 episodes and 500,000 play events for nothing. The
statement now updates a row only when one of its stored columns actually
differs, so the cost of a second sync no longer grows with the history.
"""

from __future__ import annotations

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    episode,
    history_entry,
    movie,
    season,
    series,
)
from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


def dataset(events: int = 2) -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet"), movie("101", "The Gray Man")]
    data.items["2"] = [series("200", "Lost in Space")]
    data.children["200"] = [season("210", "200", 1)]
    data.children["210"] = [episode("300", "200", "210", 1)]
    data.history = [
        history_entry("100", 1, 1_700_000_000 - 60 * number, 6_000_000, 6_000_000)
        for number in range(events)
    ]
    return data


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "curator.db")
    yield connection
    connection.close()


def client_for(server) -> PlexClient:
    return PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=100,
        rate_limit_per_second=0,
        backoff_seconds=0.0,
    )


@pytest.mark.parametrize("events", [2, 60], ids=["short-history", "long-history"])
def test_a_second_identical_sync_does_not_rewrite_the_stored_history(conn, events):
    data = dataset(events)
    with FakePlexServer(data) as server:
        PlexSync(client_for(server), conn).run()
        written = conn.total_changes
        PlexSync(client_for(server), conn).run()
        second = conn.total_changes - written

    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == events
    # What a second sync still writes is the presence pass of the complete
    # movie section (mark absent, then restore), and it does not grow with
    # the stored history.
    assert second == 2 * len(data.items["1"])


def test_a_changed_row_is_still_updated(conn):
    data = dataset()
    with FakePlexServer(data) as server:
        PlexSync(client_for(server), conn).run()
        written = conn.total_changes
        data.items["1"][0]["title"] = "Tenet (2020)"
        data.items["1"][0]["rating"] = 6.4
        PlexSync(client_for(server), conn).run()

    assert conn.total_changes > written
    assert conn.execute(
        "SELECT title, rating FROM media_items WHERE plex_rating_key = '100'"
    ).fetchone()[:] == ("Tenet (2020)", 6.4)


def test_a_returning_item_is_restored_from_absence(conn):
    """The conditional update still records a presence change."""
    data = dataset()
    with FakePlexServer(data) as server:
        PlexSync(client_for(server), conn).run()
        data.items["1"] = [data.items["1"][1]]  # Tenet leaves the server
        PlexSync(client_for(server), conn).run()
        assert conn.execute(
            "SELECT plex_present FROM media_items WHERE plex_rating_key = '100'"
        ).fetchone()[0] == 0

        data.items["1"].insert(0, movie("100", "Tenet"))
        PlexSync(client_for(server), conn).run()

    assert conn.execute(
        "SELECT plex_present FROM media_items WHERE plex_rating_key = '100'"
    ).fetchone()[0] == 1


def test_a_new_history_event_is_still_written(conn):
    """The conditional update never swallows an event that is really new."""
    data = dataset()
    with FakePlexServer(data) as server:
        PlexSync(client_for(server), conn).run()
        data.history.insert(
            0, history_entry("100", 1, 1_700_000_600, 10, 6_000_000)
        )
        PlexSync(client_for(server), conn).run()

    assert [tuple(row) for row in conn.execute(
        "SELECT played_at, position_ms FROM play_events ORDER BY played_at DESC LIMIT 1"
    )] == [("2023-11-14T22:23:20+00:00", 10)]
    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 3
