"""Protection invariants: never marks, re-checkable, ordered, precedence."""

from __future__ import annotations

from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library


def row_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_removing_a_protection_never_marks_the_item(conn):
    library_id = seed_library(conn, "3", "Filmoteca", protected=True)
    item = seed_item(conn, library_id, "300", "Blade Runner")
    candidate = make_candidate(item, "300", "Blade Runner")
    engine = ProtectionEngine(conn, settings=default_settings())

    assert engine.protection_for(candidate) is not None

    # Undo "Keep forever"...
    engine.keep_forever(candidate)
    engine.undo_keep_forever(candidate)
    assert engine.protection_for(candidate).kind is ProtectionKind.LIBRARY

    # ...unprotect the library...
    conn.execute("UPDATE libraries SET protected = 0 WHERE id = ?", (library_id,))
    conn.commit()

    # The item is only a candidate again: nothing was ever marked, no
    # candidate row was written, no removal exists.
    outcome = engine.protect([candidate])
    assert outcome.candidates == (candidate,)
    assert outcome.protected == ()
    assert row_count(conn, "removal_requests") == 0
    assert row_count(conn, "candidates") == 0


def test_protection_for_returns_the_scan_time_answer_and_none_for_the_unprotected(conn):
    protected_library = seed_library(conn, "3", "Filmoteca", protected=True)
    open_library = seed_library(conn, "1", "Movies")
    protected_item = seed_item(conn, protected_library, "300", "Blade Runner")
    open_item = seed_item(conn, open_library, "100", "Tenet")
    protected_candidate = make_candidate(protected_item, "300", "Blade Runner")
    open_candidate = make_candidate(open_item, "100", "Tenet")

    engine = ProtectionEngine(conn, settings=default_settings())
    outcome = engine.protect([protected_candidate, open_candidate])

    # The re-check entry point answers exactly what the scan-time
    # filter found — value-equal, not just kind-equal.
    (entry,) = outcome.protected
    assert engine.protection_for(protected_candidate) == entry.protection
    assert engine.protection_for(open_candidate) is None

    # A fresh engine, as the final re-check will build one, agrees.
    recheck = ProtectionEngine(conn, settings=default_settings())
    assert recheck.protection_for(protected_candidate) == entry.protection
    assert recheck.protection_for(open_candidate) is None


def test_protection_for_detects_a_protection_added_after_the_scan(conn):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    engine = ProtectionEngine(conn, settings=default_settings())

    # Unprotected at scan time, protected at re-check time: the answer
    # follows the database, and the re-check path must see it.
    outcome = engine.protect([candidate])
    assert outcome.candidates == (candidate,)

    engine.keep_forever(candidate)
    assert engine.protection_for(candidate).kind is ProtectionKind.KEEP_FOREVER


def test_unprotected_candidates_pass_through_unchanged_in_order(conn):
    open_library = seed_library(conn, "1", "Movies")
    first = make_candidate(seed_item(conn, open_library, "100", "Tenet"), "100", "Tenet")
    second = make_candidate(seed_item(conn, open_library, "101", "Oldboy"), "101", "Oldboy")
    third = make_candidate(seed_item(conn, open_library, "102", "Sunset"), "102", "Sunset")

    engine = ProtectionEngine(conn, settings=default_settings())
    outcome = engine.protect([first, second, third])

    assert outcome.candidates == (first, second, third)
    assert all(
        kept is original
        for kept, original in zip(outcome.candidates, (first, second, third))
    )
    assert outcome.protected == ()


def test_precedence_is_library_then_rating_then_keep_forever(conn):
    library_id = seed_library(conn, "3", "Filmoteca", protected=True)
    item = seed_item(conn, library_id, "300", "Severance", rating=8.7)
    candidate = make_candidate(item, "300", "Severance", rating=8.7)
    engine = ProtectionEngine(conn, settings=default_settings(minimum_rating=8.0))
    engine.keep_forever(candidate)

    assert engine.protection_for(candidate).kind is ProtectionKind.LIBRARY

    conn.execute("UPDATE libraries SET protected = 0 WHERE id = ?", (library_id,))
    conn.commit()
    assert engine.protection_for(candidate).kind is ProtectionKind.RATING

    without_rating = ProtectionEngine(
        conn, settings=default_settings(rating_protection_enabled=False)
    )
    assert without_rating.protection_for(candidate).kind is ProtectionKind.KEEP_FOREVER
