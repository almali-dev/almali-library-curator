"""The scan reads the users once, not once per candidate (issue #8)."""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, movie

from scenarios import scan, sync_dataset, ts


def test_scan_reads_the_users_once_for_every_candidate(conn):
    data = FakePlexDataset()
    data.accounts = [
        {"id": 1, "name": "Marc"},
        {"id": 2, "name": "Anna"},
    ]
    data.items["1"] = [
        movie(str(100 + index), f"Film {index}", addedAt=ts(2024, 4, 1))
        for index in range(5)
    ]
    sync_dataset(conn, data)

    statements = []
    conn.set_trace_callback(statements.append)
    candidates = scan(conn)
    conn.set_trace_callback(None)

    assert len(candidates) == 5
    assert sum(1 for statement in statements if "FROM users" in statement) == 1
