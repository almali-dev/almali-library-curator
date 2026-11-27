"""S5 (#148): the scan's snapshot reads its candidates in bounded statements.

``candidate_snapshot`` used to run about three statements per candidate — the
item's display facts, the item again for its size and disk, and its episodes —
so a scan of a few thousand candidates spent its time in the database. The
rows it writes are the same: the batching only changes how many statements
they cost.
"""

from __future__ import annotations

import pytest

from almali_library_curator.db import init_db
from almali_library_curator.detection.models import Unit
from almali_library_curator.protection.models import (
    ProtectedCandidate, Protection, ProtectionKind, ProtectionOutcome,
)
from almali_library_curator.scan.snapshot import candidate_snapshot, detection_snapshot
from lifecycle_helpers import make_candidate, seed_episode, seed_item, seed_library

GB = 1024 ** 3


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "scan.db")
    yield connection
    connection.close()


def seed(conn, candidates, library=None):
    """One library, one item per candidate, with episodes for the series."""
    library = library or seed_library(conn, "1", "Library")
    ids = {}
    for index, (unit, seasons) in enumerate(candidates, start=1):
        key = f"k{index}"
        if unit is Unit.MOVIE:
            item = seed_item(conn, library, key, f"Item {index}")
            conn.execute("UPDATE media_items SET disk = 'disk3', rating = 7.0,"
                         " rating_source = 'plex', last_played_at = '2025-01-01T12:00:00+00:00',"
                         " thumb = '/library/metadata/1/thumb/1', year = 2020 WHERE id = ?",
                         (item,))
        else:
            item = seed_item(conn, library, key, f"Item {index}", item_type="series")
            for season in (1, 2):
                seed_item(conn, library, f"{key}s{season}", f"Season {season}",
                          item_type="season", parent_rating_key=key, season_number=season)
                for episode in (1, 2):
                    episode_key = seed_episode(conn, key, season, episode, size_bytes=GB)
                    conn.execute("UPDATE episodes SET disk = ? WHERE plex_rating_key = ?",
                                 (f"disk{season}", episode_key))
        conn.commit()
        ids[index] = item
    return library, ids


def outcome_for(ids, units):
    candidates = tuple(
        make_candidate(ids[index], f"k{index}", f"Item {index}", unit=unit,
                       seasons=seasons if unit is Unit.SEASONS else ())
        for index, (unit, seasons) in enumerate(units, start=1))
    protected = (ProtectedCandidate(
        candidate=make_candidate(ids[1], "k1", "Item 1"),
        protection=Protection(kind=ProtectionKind.RATING, media_item_id=ids[1]),
        reason="Rated 9.0"),)
    return ProtectionOutcome(candidates=candidates, protected=protected)


def traced(conn, action):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, statements


def test_the_snapshot_cost_does_not_grow_with_the_number_of_candidates(conn):
    units = ([(Unit.MOVIE, ())] * 3
             + [(Unit.SERIES, ())] * 2 + [(Unit.SEASONS, (1,))] * 2)
    more = units + [(Unit.MOVIE, ())] * 10 + [(Unit.SERIES, ())] * 4
    _, ids = seed(conn, more)
    _, few = traced(conn, lambda: detection_snapshot(conn, outcome_for(ids, units)))
    _, many = traced(conn, lambda: detection_snapshot(conn, outcome_for(ids, more)))
    assert len(many) == len(few), (len(few), len(many))
    # Two bounded reads: the items of the page, and the episodes of its series.
    assert len(few) <= 2, few


def test_the_snapshot_no_longer_reads_one_candidate_at_a_time(conn):
    units = [(Unit.MOVIE, ()), (Unit.SERIES, ()), (Unit.SEASONS, (2,))]
    _, ids = seed(conn, units)
    snapshot, statements = traced(
        conn, lambda: detection_snapshot(conn, outcome_for(ids, units)))
    assert snapshot["candidates"] == 3
    lookups = [statement for statement in statements
               if "NOT IN" not in statement and "IN (" not in statement
               and ("FROM media_items WHERE id" in statement
                    or "series_rating_key = ?" in statement)]
    assert lookups == []


def test_the_batched_rows_are_the_rows_of_one_candidate_at_a_time(conn):
    """S5 keeps the exact snapshot: same keys, same values, same order."""
    units = [(Unit.MOVIE, ()), (Unit.SERIES, ()), (Unit.SEASONS, (2,))]
    _, ids = seed(conn, units)
    outcome = outcome_for(ids, units)
    batched = detection_snapshot(conn, outcome)
    one_by_one = {
        "candidates": len(outcome.candidates),
        "protected": len(outcome.protected),
        "candidate_rows": [candidate_snapshot(conn, row) for row in outcome.candidates],
        "protected_rows": [
            dict(candidate_snapshot(conn, row.candidate),
                 protection_kind=row.protection.kind.value,
                 protection_reason=row.reason,
                 protection_reason_parts=[dict(part) for part in row.reason_parts])
            for row in outcome.protected],
    }
    assert batched == one_by_one
    # The facts the page reads are still there, per unit.
    movie, series, seasons_row = batched["candidate_rows"]
    assert (movie["size_bytes"], movie["disk"], movie["last_played_at"]) == (
        24 * 1024 ** 3, "disk3", "2025-01-01T12:00:00+00:00")
    assert movie["thumb"] == "/library/metadata/1/thumb/1"
    # The size is the detection engine's own figure; the disk label is the
    # one scope_storage reports for that unit's scope.
    assert series["size_bytes"] == movie["size_bytes"] == 24 * 1024 ** 3
    assert series["disk"] == "disk1, disk2"
    assert seasons_row["disk"] == "disk2"


def test_a_candidate_with_no_item_row_still_gets_a_degraded_row(conn):
    """A preview can name an item the live table never stored: never a crash."""
    units = [(Unit.MOVIE, ()), (Unit.SERIES, ())]
    _, ids = seed(conn, units)
    outcome = ProtectionOutcome(
        candidates=(make_candidate(999, "gone", "Gone"),), protected=())
    snapshot = detection_snapshot(conn, outcome)
    [row] = snapshot["candidate_rows"]
    assert (row["size_bytes"], row["disk"], row["thumb"], row["imdb_url"]) == (
        24 * 1024 ** 3, None, None, None)
    assert row["last_played_at"] is None and row["rating_source"] is None


def test_the_disk_of_a_row_is_the_one_scope_storage_reports(conn):
    """S5 keeps the answer scope_storage gives, read once instead of twice."""
    from almali_library_curator.scan.snapshot import scope_storage

    units = [(Unit.MOVIE, ()), (Unit.SERIES, ()), (Unit.SEASONS, (2,))]
    _, ids = seed(conn, units)
    outcome = outcome_for(ids, units)
    rows = detection_snapshot(conn, outcome)["candidate_rows"]
    assert [(row["disk"],
             scope_storage(conn, row["media_item_id"],
                           outcome.candidates[index].unit,
                           outcome.candidates[index].seasons)[1])
            for index, row in enumerate(rows)] == [
        ("disk3", "disk3"), ("disk1, disk2", "disk1, disk2"), ("disk2", "disk2")]
