"""Kept forever: the row-scoped, persisted protection (S4)."""

from __future__ import annotations

from almali_library_curator.detection.models import Unit
from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library


def series_scenario(conn):
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    item = seed_item(conn, library_id, "700", "Lost in Space", item_type="series")
    return (
        make_candidate(item, "700", "Lost in Space", unit=Unit.SERIES),
        make_candidate(
            item, "700", "Lost in Space S2–S3", unit=Unit.SEASONS, seasons=(2, 3)
        ),
        make_candidate(item, "700", "Lost in Space S2", unit=Unit.SEASONS, seasons=(2,)),
    )


def test_keep_forever_protects_the_candidate_and_undo_restores_it(conn):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.keep_forever(candidate)
    outcome = engine.protect([candidate])
    assert outcome.candidates == ()
    (entry,) = outcome.protected
    assert entry.protection.kind is ProtectionKind.KEEP_FOREVER
    assert entry.protection.media_item_id == candidate.media_item_id
    assert entry.reason == "Kept forever by hand."

    engine.undo_keep_forever(candidate)
    assert engine.protection_for(candidate) is None
    assert engine.protect([candidate]).candidates == (candidate,)


def test_keep_forever_is_idempotent(conn):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.keep_forever(candidate)
    engine.keep_forever(candidate)

    rows = conn.execute(
        "SELECT COUNT(*) FROM protections WHERE kind = 'keep_forever'"
    ).fetchone()[0]
    assert rows == 1


def test_a_kept_forever_seasons_row_protects_exactly_those_seasons(conn):
    series, seasons_2_3, season_2 = series_scenario(conn)
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.keep_forever(seasons_2_3)

    assert engine.protection_for(seasons_2_3) is not None
    assert engine.protection_for(seasons_2_3).kind is ProtectionKind.KEEP_FOREVER
    # Not other seasons of the same series...
    assert engine.protection_for(season_2) is None
    # ...and not the series as a whole.
    assert engine.protection_for(series) is None


def test_a_kept_forever_series_row_does_not_protect_its_seasons(conn):
    series, seasons_2_3, _ = series_scenario(conn)
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.keep_forever(series)

    assert engine.protection_for(series).kind is ProtectionKind.KEEP_FOREVER
    assert engine.protection_for(seasons_2_3) is None


def test_undo_keep_forever_removes_only_that_scope(conn):
    _, seasons_2_3, season_2 = series_scenario(conn)
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.keep_forever(seasons_2_3)
    engine.keep_forever(season_2)
    engine.undo_keep_forever(seasons_2_3)

    assert engine.protection_for(seasons_2_3) is None
    assert engine.protection_for(season_2).kind is ProtectionKind.KEEP_FOREVER
