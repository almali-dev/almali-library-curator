"""#74: a later import asks Tautulli only for the days it does not have yet.

Tautulli's own ``after`` filter carries the window, and the stored unique
index still deduplicates the overlap, so nothing is imported twice and
nothing is missed because of a date boundary.
"""

from __future__ import annotations

import pytest

from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row
from almali_library_curator.db import init_db
from almali_library_curator.enrichment import import_tautulli_history
from almali_library_curator.tautulli import TautulliClient, TautulliUnavailableError

CURSOR_KEY = "tautulli_history_date"
APRIL = 1_711_929_600  # 2024-04-01
MAY = 1_714_521_600  # 2024-05-01


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


def cursor(conn) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return None if row is None else row[0]


def test_a_second_import_asks_only_for_the_days_since_the_last_entry(conn):
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert import_tautulli_history(conn, client) == 1
        assert "after" not in server.requests_for("get_history")[0]["body"]
        assert cursor(conn) == "2024-04-01"

        data.history.append(history_row(2, 100, 1, "Marc", date=MAY, started=MAY))
        assert import_tautulli_history(conn, client) == 1
        assert server.requests_for("get_history")[-1]["body"]["after"] == "2024-03-31"

        # The same day is asked for again, so a play stored late is not lost;
        # what was already imported is not inserted a second time.
        assert import_tautulli_history(conn, client) == 0
        assert server.requests_for("get_history")[-1]["body"]["after"] == "2024-04-30"

    rows = conn.execute(
        "SELECT plex_rating_key, played_at, source FROM play_events ORDER BY played_at"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("100", "2024-04-01T00:00:00+00:00", "tautulli"),
        ("100", "2024-05-01T00:00:00+00:00", "tautulli"),
    ]


def test_an_empty_answer_leaves_the_cursor_alone(conn):
    data = FakeTautulliDataset()
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert import_tautulli_history(conn, client) == 0
        assert cursor(conn) is None
        data.history = [history_row(1, 100, 1, "Marc")]
        assert import_tautulli_history(conn, client) == 1
        assert "after" not in server.requests_for("get_history")[-1]["body"]


def test_a_failed_import_leaves_the_cursor_where_it_was(conn):
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert import_tautulli_history(conn, client) == 1
        data.fail_always("get_history", 503)
        with pytest.raises(TautulliUnavailableError):
            import_tautulli_history(conn, client)
    assert cursor(conn) == "2024-04-01"


def test_a_first_import_asks_for_the_whole_history_and_reports_what_it_stored(conn, caplog):
    import logging

    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 1, "Marc"), history_row(2, 999, 1, "Marc")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 1
        request = server.requests_for("get_history")[-1]
    assert "after" not in request["body"]
    assert not [record for record in caplog.records
                if record.msg == "enrichment_history_finished"][0].skipped_unknown_users
