"""#128: the rating source label names where the number really came from.

The sync stores the Plex number chosen by the Rating source setting; the
snapshot labels TMDB when detection's fallback supplies the shown number.
"""

from __future__ import annotations

import pytest

from almali_library_curator.db import init_db
from almali_library_curator.scan.snapshot import candidate_snapshot

from lifecycle_helpers import make_candidate, seed_item, seed_library


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "rating-source.db")
    yield connection
    connection.close()


def snapshot(conn, *, item_rating, rating_source, candidate_rating):
    library = seed_library(conn, "1", "Movies")
    item_id = seed_item(conn, library, "100", "Tenet", rating=item_rating)
    conn.execute("UPDATE media_items SET rating_source = ? WHERE id = ?",
                 (rating_source, item_id))
    conn.commit()
    candidate = make_candidate(item_id, "100", "Tenet", rating=candidate_rating)
    return candidate_snapshot(conn, candidate)


@pytest.mark.parametrize(
    ("item_rating", "rating_source", "candidate_rating", "expected"),
    [
        (8.7, "plex", 8.7, "Plex"),
        (8.1, "plex_audience", 8.1, "Plex audience"),
        # highest: TMDB wins over the stored Plex number, so it is labelled.
        (8.1, "plex_audience", 8.7, "TMDB"),
        (None, None, 8.0, "TMDB"),
        (None, None, None, None),
    ],
)
def test_the_label_names_the_real_source_or_nothing(
        conn, item_rating, rating_source, candidate_rating, expected):
    stored = snapshot(conn, item_rating=item_rating, rating_source=rating_source,
                      candidate_rating=candidate_rating)

    assert stored["rating_source"] == expected
