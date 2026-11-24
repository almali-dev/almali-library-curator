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


def test_a_movie_row_with_empty_optional_numbers_imports_with_the_page(conn, settings, tmp_path):
    """#125: Tautulli sends '' for the numbers a movie row has no value for.

    The model already expects no value in those fields, so the redaction is
    read as one: one such row used to refuse the whole history page, so no
    Tautulli play was ever imported.
    """
    from almali_library_curator.enrichment import run_enrichment
    from almali_library_curator.hostfs import HostFilesystem

    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.items["2"] = [series("200", "Show")]
    plex.children["200"] = [season("210", "200", 1)]
    plex.children["210"] = [episode("300", "200", "210", 1)]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    film = history_row(1, 100, 1, "Marc")
    film.update(media_type="movie", media_index="", parent_media_index="")
    data.history = [film, history_row(2, 300, 2, "Anna")]
    with FakeTautulliServer(data) as server:
        summary = run_enrichment(
            conn, settings,
            tautulli=TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0),
            hostfs=HostFilesystem(media_root=tmp_path),
        )
    assert summary.errors == {}
    assert summary.history_imported == 2
    rows = conn.execute(
        "SELECT plex_rating_key, source FROM play_events ORDER BY plex_rating_key").fetchall()
    assert [tuple(row) for row in rows] == [("100", "tautulli"), ("300", "tautulli")]


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


def _synced_users(conn):
    return {row["plex_user_id"]: row["id"] for row in
            conn.execute("SELECT id, plex_user_id FROM users")}


def test_the_owner_s_tautulli_plays_map_to_the_plex_account_by_name(conn, caplog):
    """#127: Plex calls the owner 1, Tautulli uses the owner's plex.tv id.

    Tautulli ``2755052`` matches no ``users.plex_user_id``, so the play used to
    be skipped. The Tautulli username falls back onto the Plex title, compared
    case-insensitively, so the owner's plays import like everyone else's.
    """
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.accounts = [{"id": 1, "name": "mmarkimark"}, {"id": 2, "name": "Anna"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    owner_id = _synced_users(conn)["1"]
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 2755052, "mmarkimark"),
                    history_row(2, 100, 2, "anna")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 2
    rows = conn.execute("SELECT user_id FROM play_events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [owner_id, _synced_users(conn)["2"]]
    record = next(r for r in caplog.records if r.msg == "enrichment_history_finished")
    assert record.skipped_unknown_users == 0
    assert record.skipped_unknown_user_plays == {}


def test_a_matching_user_id_wins_over_a_matching_username(conn):
    """#127: the id is the account's own identity, so it decides first."""
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.accounts = [{"id": 1, "name": "mmarkimark"}, {"id": 2, "name": "Anna"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    # The row's id names Anna, while its username names the owner.
    data.history = [history_row(1, 100, 2, "mmarkimark")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert import_tautulli_history(conn, client) == 1
    assert conn.execute("SELECT user_id FROM play_events").fetchone()[0] == _synced_users(conn)["2"]


def test_an_unmatched_tautulli_user_is_skipped_and_named_in_the_log(conn, caplog):
    """#127: no Plex account carries that id or that name, so it is skipped."""
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.accounts = [{"id": 1, "name": "mmarkimark"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 2755052, "mmarkimark"),
                    history_row(2, 100, 169540618, "MerceBon"),
                    history_row(3, 100, 169540618, "MerceBon")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 1
    record = next(r for r in caplog.records if r.msg == "enrichment_history_finished")
    assert record.skipped_unknown_users == 2
    assert record.skipped_unknown_user_plays == {"MerceBon": 2}


def test_skipped_plays_are_reported_for_at_most_ten_tautulli_users(conn, caplog):
    """#127: the log line stays bounded, and names the heaviest skip first."""
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.accounts = [{"id": 1, "name": "mmarkimark"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    data = FakeTautulliDataset()
    data.history = [history_row(index, 100, 10_000 + index, f"user{name:02d}")
                    for name in range(12) for index in range(name + 1)]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        with caplog.at_level(logging.INFO):
            assert import_tautulli_history(conn, client) == 0
    record = next(r for r in caplog.records if r.msg == "enrichment_history_finished")
    assert record.skipped_unknown_users == sum(range(1, 13))
    assert record.skipped_unknown_user_plays == {
        f"user{name:02d}": name + 1 for name in range(2, 12)}


def test_an_excluded_user_s_tautulli_plays_are_still_imported(conn):
    """Detection owns the exclusion, so the history keeps every known account."""
    from almali_library_curator.enrichment import import_tautulli_history
    plex = FakePlexDataset()
    plex.items["1"] = [movie("100", "Movie")]
    plex.accounts = [{"id": 1, "name": "mmarkimark"}, {"id": 2, "name": "Anna"}]
    with FakePlexServer(plex) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()
    excluded_id = _synced_users(conn)["2"]
    conn.execute("UPDATE users SET excluded = 1 WHERE id = ?", (excluded_id,))
    conn.commit()
    data = FakeTautulliDataset()
    data.history = [history_row(1, 100, 2, "Anna")]
    with FakeTautulliServer(data) as server:
        client = TautulliClient(server.base_url, "test-api-key", rate_limit_per_second=0)
        assert import_tautulli_history(conn, client) == 1
    assert conn.execute("SELECT user_id FROM play_events").fetchone()[0] == excluded_id
