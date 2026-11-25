"""#112: every sync reads the whole Plex play history.

#74 read only what followed the newest play already stored: the history was
asked for newest first and the read ended on the first page that carried
nothing newer than that cursor. A play Plex stores late with an older
``viewedAt`` (an offline play synced afterwards) lands behind that point and
was never imported. The whole history is now read on every sync, and the
unique index on the stored events plus the unchanged-row check keep a second
read of the same history from storing anything twice.

This file keeps the name it had when it pinned the incremental read; the
tests below pin the full read that replaced it.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, history_entry, movie
from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync, PlexUnavailableError

HISTORY_PATH = "/status/sessions/history/all"
NEWEST = 1_700_000_000

#: The columns of a stored play, without the id every read creates its own of.
PLAY_COLUMNS = (
    "user_id, plex_rating_key, watched, played_at, position_ms, duration_ms, source"
)


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


def events(count: int) -> list[dict]:
    """``count`` plays, a minute apart, newest first."""
    return [
        history_entry("100", 1, NEWEST - 60 * number, 6_000_000, 6_000_000)
        for number in range(count)
    ]


def plays(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute(
        f"SELECT {PLAY_COLUMNS} FROM play_events ORDER BY played_at, id"
    )]


def test_a_play_stored_late_with_an_older_instant_is_imported_on_the_next_sync(
    dataset, conn
):
    """Rule (a) of #112: the newest stored play is not the end of the history.

    An offline play reaches Plex afterwards with the instant it happened at,
    so it is older than every play already stored. Starting the next read
    from the newest stored play would leave it behind for good.
    """
    dataset.history = events(6)
    late = NEWEST - 86_400  # A day before the oldest play of the first sync.
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert len(plays(conn)) == 6

        # Plex stored the play late; its instant is where it happened.
        dataset.history.append(
            history_entry("100", 1, late, 6_000_000, 6_000_000)
        )
        PlexSync(client_for(server), conn).run()

    stored = plays(conn)
    assert len(stored) == 7
    # The late play is the oldest of the stored ones, read and only once.
    assert [row[3] for row in stored][0] == "2023-11-13T22:13:20+00:00"


def test_every_sync_asks_for_every_page_of_the_history(dataset, conn):
    """The whole history is read on every sync, not only its newest pages."""
    dataset.history = events(20)  # ten pages of two
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 10

        PlexSync(client_for(server), conn).run()
        assert len(server.requests_for(HISTORY_PATH)) == 20
        # No order is asked for: the read is the whole history whatever
        # order the server answers in.
        assert not [
            request for request in server.requests_for(HISTORY_PATH)
            if "sort" in request["query"]
        ]

    # No read cursor is kept, so a later sync cannot start from one.
    keys = [row[0] for row in conn.execute("SELECT key FROM app_meta")]
    assert not [key for key in keys if key.startswith("plex_history_viewed_at")]


def test_reading_the_same_history_again_stores_no_play_twice(dataset, conn):
    """Rule (c): a second full read changes no stored play and no count."""
    dataset.history = events(6)
    with FakePlexServer(dataset) as server:
        first = PlexSync(client_for(server), conn).run()
        before = plays(conn)
        second = PlexSync(client_for(server), conn).run()

    assert plays(conn) == before
    assert first.play_events == second.play_events == len(before) == 6
    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 6


def test_a_history_answered_oldest_first_is_read_the_same_way(dataset, conn):
    """Nothing is trusted about the order: every entry is stored either way."""
    dataset.history = list(reversed(events(20)))
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()

    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 20


def test_a_failed_history_read_stores_no_play_of_it(dataset, conn):
    dataset.history = events(1)
    with FakePlexServer(dataset) as server:
        PlexSync(client_for(server), conn).run()
        dataset.history.extend(events(3)[1:])
        dataset.always_fail[HISTORY_PATH] = 503
        with pytest.raises(PlexUnavailableError):
            PlexSync(client_for(server), conn).run()

    assert len(plays(conn)) == 1


def test_a_history_entry_without_a_rating_key_is_skipped(dataset, conn):
    """#118: Plex keeps the plays of a removed item without linking any item.

    Such an entry cannot match a candidate, so it is skipped instead of
    failing the whole sync.
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
    assert [row[1] for row in plays(conn)] == ["100"]
