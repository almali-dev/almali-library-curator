"""The protection pass reads its facts once, not once per candidate (issue #8)."""

from __future__ import annotations

from almali_library_curator.protection import ProtectionEngine

from helpers import default_settings, make_candidate, seed_item, seed_library


def test_protect_reads_the_libraries_and_the_protections_once(conn):
    library_id = seed_library(conn, "1", "Movies")
    candidates = [
        make_candidate(seed_item(conn, library_id, str(100 + index), f"Film {index}"),
                       str(100 + index), f"Film {index}")
        for index in range(5)
    ]
    engine = ProtectionEngine(conn, settings=default_settings())

    statements = []
    conn.set_trace_callback(statements.append)
    outcome = engine.protect(candidates)
    conn.set_trace_callback(None)

    assert len(outcome.candidates) == 5
    assert sum(1 for statement in statements if "FROM protections" in statement) == 1
    assert sum(1 for statement in statements if "AND media_item_id = ?" in statement) == 0
    assert sum(
        1 for statement in statements
        if "SELECT library_id FROM media_items WHERE id" in statement
    ) == 0


def test_protect_reads_only_the_items_of_the_pass(conn):
    library_id = seed_library(conn, "1", "Movies")
    seed_item(conn, library_id, "200", "Another film")
    item = seed_item(conn, library_id, "100", "Film")
    candidate = make_candidate(item, "100", "Film")
    engine = ProtectionEngine(conn, settings=default_settings())

    statements = []
    conn.set_trace_callback(statements.append)
    engine.protect([candidate])
    conn.set_trace_callback(None)

    media_queries = [s for s in statements if "FROM media_items" in s]
    assert media_queries
    assert all("WHERE id IN" in statement for statement in media_queries)
