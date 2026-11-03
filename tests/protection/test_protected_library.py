"""Protected libraries: the database flag and the settings names (S4)."""

from __future__ import annotations

from almali_library_curator.detection.models import Category
from almali_library_curator.protection import ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library


def test_candidate_in_protected_library_is_listed_protected(conn):
    library_id = seed_library(conn, "3", "Filmoteca", protected=True)
    open_library = seed_library(conn, "1", "Movies")
    protected_item = seed_item(conn, library_id, "300", "Blade Runner")
    open_item = seed_item(conn, open_library, "100", "Tenet")
    protected_candidate = make_candidate(protected_item, "300", "Blade Runner")
    open_candidate = make_candidate(open_item, "100", "Tenet")

    from almali_library_curator.protection import ProtectionEngine

    engine = ProtectionEngine(conn, settings=default_settings())
    outcome = engine.protect([protected_candidate, open_candidate])

    # The protected item is not a candidate any more.
    assert outcome.candidates == (open_candidate,)
    (entry,) = outcome.protected
    assert entry.candidate is protected_candidate
    assert entry.protection.kind is ProtectionKind.LIBRARY
    assert entry.protection.library_id == library_id
    assert entry.protection.detail == "Filmoteca"
    # The Protected view shows what protects it...
    assert entry.reason == "The protected library Filmoteca covers it."
    # ...and the category it would otherwise match stays visible.
    assert entry.candidate.categories == (Category.NEVER_WATCHED,)


def test_settings_protect_a_library_by_plex_key_exactly(conn):
    library_id = seed_library(conn, "3", "Filmoteca")
    item = seed_item(conn, library_id, "300", "Blade Runner")
    candidate = make_candidate(item, "300", "Blade Runner")

    from almali_library_curator.protection import ProtectionEngine

    engine = ProtectionEngine(
        conn, settings=default_settings(protected_libraries=("3",))
    )
    assert engine.protection_for(candidate) is not None

    # A name matching neither a plex key nor a title protects nothing.
    other = ProtectionEngine(
        conn, settings=default_settings(protected_libraries=("9",))
    )
    assert other.protection_for(candidate) is None


def test_settings_protect_a_library_by_title_case_insensitively(conn):
    library_id = seed_library(conn, "3", "Filmoteca")
    item = seed_item(conn, library_id, "300", "Blade Runner")
    candidate = make_candidate(item, "300", "Blade Runner")

    from almali_library_curator.protection import ProtectionEngine

    engine = ProtectionEngine(
        conn, settings=default_settings(protected_libraries=("filmoteca",))
    )
    assert engine.protection_for(candidate) is not None


def test_library_is_unprotected_when_neither_source_names_it(conn):
    library_id = seed_library(conn, "3", "Filmoteca", protected=True)
    item = seed_item(conn, library_id, "300", "Blade Runner")
    candidate = make_candidate(item, "300", "Blade Runner")

    from almali_library_curator.protection import ProtectionEngine

    flagged = ProtectionEngine(conn, settings=default_settings())
    assert flagged.protection_for(candidate) is not None

    # The flag alone protects even when the settings do not name it.
    conn.execute("UPDATE libraries SET protected = 0 WHERE id = ?", (library_id,))
    conn.commit()
    unnamed = ProtectionEngine(conn, settings=default_settings())
    assert unnamed.protection_for(candidate) is None

    # The settings name alone protects even when the flag is clear.
    named = ProtectionEngine(
        conn, settings=default_settings(protected_libraries=("Filmoteca",))
    )
    assert named.protection_for(candidate) is not None


def test_protected_library_ids_reports_the_combined_set(conn):
    flagged_id = seed_library(conn, "3", "Filmoteca", protected=True)
    named_id = seed_library(conn, "4", "Cinema")
    open_id = seed_library(conn, "1", "Movies")

    from almali_library_curator.protection import ProtectionEngine

    engine = ProtectionEngine(
        conn, settings=default_settings(protected_libraries=("Cinema",))
    )
    assert engine.protected_library_ids() == (flagged_id, named_id)
    assert open_id not in engine.protected_library_ids()
