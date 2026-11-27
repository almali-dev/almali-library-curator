"""S6 (#139): the published snapshot carries the item's stored poster thumb.

The Library's rows are built from this snapshot, so a dry run (whose
``media_items`` exist only in the in-memory preview) reaches the screen
through it. A seasons row carries its series' ``media_item_id``, so it
carries the series' poster too.
"""

from __future__ import annotations

import pytest

from lifecycle_helpers import make_candidate, seed_item, seed_library
from almali_library_curator.db import init_db
from almali_library_curator.scan.snapshot import candidate_snapshot

MOVIE_THUMB = "/library/metadata/11/thumb/1700000000"
SERIES_THUMB = "/library/metadata/200/thumb/1700000001"


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "almali-library-curator.db")
    yield connection
    connection.close()


def stored_thumb(conn, media_item_id, thumb):
    conn.execute("UPDATE media_items SET thumb = ? WHERE id = ?", (thumb, media_item_id))
    conn.commit()


def test_a_movie_candidate_snapshot_carries_its_own_thumb(conn):
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    stored_thumb(conn, item, MOVIE_THUMB)

    snapshot = candidate_snapshot(conn, make_candidate(item, "11", "Tenet"))

    assert snapshot["thumb"] == MOVIE_THUMB


def test_a_seasons_candidate_carries_the_series_thumb(conn):
    """A seasons row's media_item_id is the series, so it shows its poster."""
    library = seed_library(conn, "2", "Shows", section_type="show")
    series = seed_item(conn, library, "200", "Lost in Space", item_type="series")
    stored_thumb(conn, series, SERIES_THUMB)

    snapshot = candidate_snapshot(
        conn, make_candidate(series, "200", "Lost in Space", seasons=(1,)))

    assert snapshot["thumb"] == SERIES_THUMB


def test_a_candidate_with_no_stored_thumb_carries_none(conn):
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")

    snapshot = candidate_snapshot(conn, make_candidate(item, "11", "Tenet"))

    assert snapshot["thumb"] is None


def test_a_candidate_whose_item_row_is_gone_carries_none(conn):
    """A dry run's preview may hold the candidate without the item row."""
    snapshot = candidate_snapshot(conn, make_candidate(999, "999", "Gone"))

    assert snapshot["thumb"] is None
