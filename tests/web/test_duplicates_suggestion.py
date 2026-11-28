"""#133 T4: the Duplicates view knows each version's identity and the one
the app suggests keeping (S2: the highest resolution). Nothing visible
changes yet; the view model carries what the removal screen will need."""

from __future__ import annotations

from almali_library_curator.web.views import duplicates_view
from test_duplicates import seed_library_row, seed_movie


def seed_version(conn, item, position, media_id, resolution, path):
    conn.execute(
        "INSERT INTO media_versions (media_item_id, position, plex_media_id, resolution,"
        " size_bytes, file_path) VALUES (?, ?, ?, ?, 10, ?)",
        (item, position, media_id, resolution, path))
    conn.commit()


def test_each_version_carries_its_media_id_its_file_and_one_suggestion(conn):
    library = seed_library_row(conn, "1", "Movies")
    heat = seed_movie(conn, library, "11", "Heat")
    seed_version(conn, heat, 0, 501, "1080", "/m/Heat 1080p.mkv")
    seed_version(conn, heat, 1, 502, "4k", "/m/Heat 2160p.mkv")

    [group] = duplicates_view(conn, None).versions

    assert [(c.plex_media_id, c.file_path, c.suggested_keep) for c in group.copies] == [
        (501, "/m/Heat 1080p.mkv", False),
        (502, "/m/Heat 2160p.mkv", True),
    ]


def test_no_version_is_suggested_when_no_resolution_is_known(conn):
    library = seed_library_row(conn, "1", "Movies")
    heat = seed_movie(conn, library, "11", "Heat")
    seed_version(conn, heat, 0, 501, None, "/m/a.mkv")
    seed_version(conn, heat, 1, 502, None, "/m/b.mkv")

    [group] = duplicates_view(conn, None).versions

    assert [c.suggested_keep for c in group.copies] == [False, False]
