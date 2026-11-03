"""High-rating protection: at or above the minimum, switchable (S4)."""

from __future__ import annotations

from almali_library_curator.detection.models import Unit
from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library


def rating_scenario(conn, rating: float | None):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "Severance", rating=rating)
    return make_candidate(item, "100", "Severance", rating=rating)


def test_rating_at_or_above_the_minimum_is_protected(conn):
    candidate = rating_scenario(conn, 8.7)

    engine = ProtectionEngine(conn, settings=default_settings())
    outcome = engine.protect([candidate])

    assert outcome.candidates == ()
    (entry,) = outcome.protected
    assert entry.protection.kind is ProtectionKind.RATING
    assert entry.protection.media_item_id == candidate.media_item_id
    assert entry.reason == "Rated 8.7, at or above the minimum of 8.0."


def test_rating_exactly_at_the_minimum_is_protected(conn):
    candidate = rating_scenario(conn, 8.0)

    engine = ProtectionEngine(conn, settings=default_settings())
    assert engine.protection_for(candidate) is not None


def test_rating_below_the_minimum_is_not_protected(conn):
    candidate = rating_scenario(conn, 7.9)

    engine = ProtectionEngine(conn, settings=default_settings())
    assert engine.protection_for(candidate) is None


def test_unknown_rating_is_not_protected(conn):
    candidate = rating_scenario(conn, None)

    engine = ProtectionEngine(conn, settings=default_settings())
    assert engine.protection_for(candidate) is None


def test_a_seasons_candidate_is_protected_by_its_rating(conn):
    library_id = seed_library(conn, "2", "Shows", section_type="show")
    item = seed_item(conn, library_id, "700", "Severance", item_type="series", rating=8.5)
    candidate = make_candidate(
        item, "700", "Severance S2–S3", unit=Unit.SEASONS, seasons=(2, 3), rating=8.5
    )

    engine = ProtectionEngine(conn, settings=default_settings())
    assert engine.protection_for(candidate) is not None
    assert engine.protection_for(candidate).kind is ProtectionKind.RATING


def test_rating_protection_can_be_switched_off(conn):
    candidate = rating_scenario(conn, 9.5)

    engine = ProtectionEngine(
        conn, settings=default_settings(rating_protection_enabled=False)
    )
    outcome = engine.protect([candidate])

    # With the rule off, the rating never protects anything.
    assert outcome.protected == ()
    assert outcome.candidates == (candidate,)
    assert engine.protection_for(candidate) is None


def test_changing_the_minimum_rating_re_evaluates_protection_without_marking(conn):
    from dataclasses import replace

    candidate = rating_scenario(conn, 8.7)
    base = default_settings()

    protected = ProtectionEngine(conn, settings=base)
    assert protected.protection_for(candidate) is not None

    # S4 table: "any item at or above it" is protected, so moving the
    # minimum above the item's rating — the protection reading of
    # "lowering the threshold" — makes it a candidate again, and marks
    # nothing on its way out.
    raised = ProtectionEngine(conn, settings=replace(base, minimum_rating=9.0))
    outcome = raised.protect([candidate])
    assert outcome.candidates == (candidate,)
    assert outcome.protected == ()
    requests = conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0]
    assert requests == 0


def test_lowering_the_minimum_rating_newly_protects_items(conn):
    from dataclasses import replace

    candidate = rating_scenario(conn, 8.7)
    base = default_settings(minimum_rating=9.0)

    strict = ProtectionEngine(conn, settings=base)
    assert strict.protection_for(candidate) is None

    relaxed = ProtectionEngine(conn, settings=replace(base, minimum_rating=8.0))
    assert relaxed.protection_for(candidate) is not None
