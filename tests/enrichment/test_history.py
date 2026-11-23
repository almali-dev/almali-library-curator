import logging

from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.tautulli import TautulliClient
from fake_plex_server import (FakePlexDataset, FakePlexServer, episode, history_entry,
                              movie, season, series)
from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row


def test_import_tautulli_maps_known_users_and_items_idempotently(conn, caplog):
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.items["2"] = [series("200", "Show")]
    plex.children["200"] = [season("210", "200", 1)]
    plex.children["210"] = [episode("300", "200", "210", 1)]
    plex.history = [history_entry("100", 1, 1711929600, 2700000, 2700000)]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    data.history = [
        history_row(1, 100, 1, "Marc"),  # Same event as Plex; unique across sources.
        history_row(2, 300, 2, "Anna", view_offset=40000, duration=100000, percent_complete=40),
        history_row(3, 100, 2, "Anna", view_offset=None, duration=100000, percent_complete=91),
        history_row(4, 100, 999, "Unknown"),
        history_row(5, 999, 1, "Marc"),
    ]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", page_size=2, rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 2
            assert import_tautulli_history(conn, client) == 0
        assert len([r for r in server.requests if r["cmd"] == "get_history"]) == 6
    rows = conn.execute("SELECT plex_rating_key, watched, source FROM play_events ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("100", 1, "plex"), ("300", 0, "tautulli"), ("100", 1, "tautulli")]
    records = [r for r in caplog.records if r.msg == "enrichment_history_finished"]
    assert all(r.skipped_unknown_users == 1 and r.skipped_unknown_items == 1 for r in records)
    assert import_tautulli_history(conn, None) == 0


def test_import_tautulli_skips_rows_with_null_ids_instead_of_failing(conn, caplog):
    """#119: one row with a null ``id`` or ``rating_key`` used to fail the import.

    The row ``id`` is never stored, so a row without it is imported like any
    other; a row without ``rating_key`` cannot be stored and counts as
    invalid. Every other row is still imported.
    """
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie"), movie("101", "Other")]
    plex.accounts = [{"id": 1, "name": "Marc"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    open_play = history_row(2, 101, 1, "Marc")
    open_play["id"] = None  # A play Tautulli has not closed yet.
    data.history = [
        history_row(1, 100, 1, "Marc"),
        open_play,
        history_row(3, None, 1, "Marc"),
    ]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 2
    rows = conn.execute("SELECT plex_rating_key FROM play_events ORDER BY plex_rating_key").fetchall()
    assert [tuple(row) for row in rows] == [("100",), ("101",)]
    record = next(r for r in caplog.records if r.msg == "enrichment_history_finished")
    assert record.skipped_invalid == 1
    assert record.skipped_unknown_items == 0
