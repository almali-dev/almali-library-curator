"""A damaged stored scope must not silently lose a protection (S4).

The ``protections.detail`` of a kept-forever row names the unit and the
exact seasons it covers. When that value cannot be read, the engine cannot
attribute it to a row: it fails closed, the item stays protected and the
owner sees the damaged protection in the Protected view.
"""

from __future__ import annotations

import logging

import pytest

from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library

# Every shape a damaged scope can take: an unknown unit, a seasons scope
# without seasons, a seasons scope with a broken number, a non-seasons unit
# carrying a separator, an empty value and a missing one.
DAMAGED_DETAILS = ["garbage", "seasons:", "seasons:x", "movie:1", "", None]


def seed_damaged_scope(conn, media_item_id: int, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO protections (kind, media_item_id, detail) VALUES (?, ?, ?)",
        ("keep_forever", media_item_id, detail),
    )
    conn.commit()


@pytest.mark.parametrize("detail", DAMAGED_DETAILS)
def test_a_damaged_stored_scope_keeps_the_item_protected(conn, detail):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    seed_damaged_scope(conn, item, detail)
    engine = ProtectionEngine(conn, settings=default_settings())

    protection = engine.protection_for(candidate)

    assert protection is not None
    assert protection.kind is ProtectionKind.KEEP_FOREVER
    outcome = engine.protect([candidate])
    assert outcome.candidates == ()
    assert outcome.protected[0].candidate is candidate
    # Protecting an item never marks it.
    assert conn.execute("SELECT COUNT(*) FROM removal_requests").fetchone()[0] == 0


def test_a_damaged_stored_scope_is_reported_in_the_log(conn, caplog):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    seed_damaged_scope(conn, item, "garbage")
    engine = ProtectionEngine(conn, settings=default_settings())

    with caplog.at_level(
        logging.WARNING, logger="almali_library_curator.protection.engine"
    ):
        engine.protection_for(candidate)

    assert "keep_forever_scope_damaged" in caplog.text


def test_undo_clears_a_damaged_stored_scope(conn):
    library_id = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    seed_damaged_scope(conn, item, "garbage")
    engine = ProtectionEngine(conn, settings=default_settings())

    engine.undo_keep_forever(candidate)

    assert engine.protection_for(candidate) is None
    assert engine.protect([candidate]).candidates == (candidate,)
    rows = conn.execute("SELECT COUNT(*) FROM protections").fetchone()[0]
    assert rows == 0


def test_a_damaged_scope_of_another_item_does_not_protect_this_candidate(conn):
    library_id = seed_library(conn, "1", "Movies")
    other = seed_item(conn, library_id, "200", "The Wire")
    item = seed_item(conn, library_id, "100", "The Office (US)")
    candidate = make_candidate(item, "100", "The Office (US)")
    seed_damaged_scope(conn, other, "garbage")
    engine = ProtectionEngine(conn, settings=default_settings())

    assert engine.protection_for(candidate) is None
    assert engine.protect([candidate]).candidates == (candidate,)
