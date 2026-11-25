"""#112: every import reads the whole Tautulli history.

#74 asked Tautulli only for the days since the newest imported one (minus a
day of overlap), so a play Tautulli stored late with an older date was behind
that window and never imported. The whole history is now asked for on every
import, and the stored unique index keeps a second read from inserting a play
twice.

This file keeps the name it had when it pinned the incremental import; the
tests below pin the full read that replaced it.
"""

from __future__ import annotations

import pytest

from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row
from almali_library_curator.db import init_db
from almali_library_curator.enrichment import import_tautulli_history
from almali_library_curator.tautulli import TautulliClient, TautulliUnavailableError

MARCH = 1_709_251_200  # 2024-03-01
APRIL = 1_711_929_600  # 2024-04-01
MAY = 1_714_521_600  # 2024-05-01

#: The columns of a stored play, without the id every import creates its own of.
PLAY_COLUMNS = (
    "user_id, plex_rating_key, watched, played_at, position_ms, duration_ms, source"
)


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "curator.db")
    connection.execute(
        "INSERT INTO libraries (plex_key, title, section_type) VALUES ('1', 'Movies', 'movie')"
    )
    connection.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type)"
        " VALUES (1, '100', 'Tenet', 'movie')"
    )
    connection.execute("INSERT INTO users (plex_user_id, title) VALUES ('1', 'Marc')")
    connection.commit()
    yield connection
    connection.close()


def client_for(server) -> TautulliClient:
    return TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)


def plays(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute(
        f"SELECT {PLAY_COLUMNS} FROM play_events ORDER BY played_at, id"
    )]


def test_a_row_stored_late_with_an_older_date_is_imported_on_the_next_import(conn):
    """Rule (b) of #112: the newest imported day is not the start of the history.

    Tautulli can receive a play afterwards with the date it happened at, so
    it is older than every row already imported. Asking only for the days
    since the newest imported one would skip it for good.
    """
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc", date=APRIL, started=APRIL)]
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        assert import_tautulli_history(conn, client) == 1

        data.history.append(history_row(2, 100, 1, "Marc", date=MARCH, started=MARCH))
        assert import_tautulli_history(conn, client) == 1

    assert [row[3] for row in plays(conn)] == [
        "2024-03-01T00:00:00+00:00",
        "2024-04-01T00:00:00+00:00",
    ]


def test_every_import_asks_for_the_whole_history(conn):
    """No date window is sent, so nothing older than a cursor can hide."""
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        assert import_tautulli_history(conn, client) == 1
        assert import_tautulli_history(conn, client) == 0
        bodies = [request["body"] for request in server.requests_for("get_history")]

    assert bodies and all("after" not in body for body in bodies)
    # No read cursor is kept either, so a later import cannot start from one.
    keys = [row[0] for row in conn.execute("SELECT key FROM app_meta")]
    assert "tautulli_history_date" not in keys


def test_reading_the_same_history_again_imports_no_play_twice(conn):
    """Rule (c): a second full read changes no stored play and no count."""
    data = FakeTautulliDataset()
    data.history = [
        history_row(1, 100, 1, "Marc", date=APRIL, started=APRIL),
        history_row(2, 100, 1, "Marc", date=MAY, started=MAY),
    ]
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        assert import_tautulli_history(conn, client) == 2
        before = plays(conn)
        assert import_tautulli_history(conn, client) == 0

    assert plays(conn) == before
    assert len(before) == 2
    assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 2


def test_an_empty_answer_stores_nothing(conn):
    data = FakeTautulliDataset()
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        assert import_tautulli_history(conn, client) == 0
        data.history = [history_row(1, 100, 1, "Marc")]
        assert import_tautulli_history(conn, client) == 1
        assert "after" not in server.requests_for("get_history")[-1]["body"]


def test_a_failed_import_stores_nothing_of_it(conn):
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        assert import_tautulli_history(conn, client) == 1
        data.fail_always("get_history", 503)
        with pytest.raises(TautulliUnavailableError):
            import_tautulli_history(conn, client)

    assert len(plays(conn)) == 1


def test_a_first_import_asks_for_the_whole_history_and_reports_what_it_stored(conn, caplog):
    import logging

    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc"), history_row(2, 999, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = client_for(server)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 1
        request = server.requests_for("get_history")[-1]
    assert "after" not in request["body"]
    assert not [record for record in caplog.records
                if record.msg == "enrichment_history_finished"][0].skipped_unknown_users
