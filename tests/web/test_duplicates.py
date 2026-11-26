"""S2 (#132): the Duplicates screen, read-only, through the HTTP client.

Two lists: the same title in more than one curated library (matched by the
TMDB/IMDb/TVDB id Plex already reports), and one item holding more than one
version. Every copy says its library, resolution, video codec, size and
bitrate. A copy a protected library or a "Keep forever" keeps stays in the
list and says so; the screen neither changes a freed-space figure nor offers
an action.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from markupsafe import Markup

from fake_plex_server import FakePlexDataset, FakePlexServer
from test_plex_versions import two_version_movie

from almali_library_curator.config import save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.web.views import duplicates_view, summary_view

GB = 1_000_000_000


def page_text(response):
    return " ".join(Markup(response.get_data(as_text=True)).striptags().split())


def seed_library_row(conn, plex_key, title, *, protected=False):
    conn.execute(
        "INSERT INTO libraries (plex_key, title, section_type, protected) VALUES (?, ?, 'movie', ?)",
        (plex_key, title, 1 if protected else 0))
    conn.execute("DELETE FROM media_items WHERE 1 = 0")  # keep sqlite in one txn shape
    conn.commit()
    return int(conn.execute("SELECT id FROM libraries WHERE plex_key = ?",
                            (plex_key,)).fetchone()[0])


def seed_movie(conn, library_id, rating_key, title, *, tmdb_id=None, imdb_id=None,
               tvdb_id=None, item_type="movie", size=None, path=None):
    conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type,"
        " tmdb_id, imdb_id, tvdb_id, file_path, file_size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (library_id, rating_key, title, item_type, tmdb_id, imdb_id, tvdb_id, path, size))
    conn.commit()
    return int(conn.execute("SELECT id FROM media_items WHERE plex_rating_key = ?",
                            (rating_key,)).fetchone()[0])


def seed_version(conn, media_item_id, position, *, resolution=None, video_codec=None,
                 bitrate_kbps=None, size_bytes=None, file_path=None):
    conn.execute(
        "INSERT INTO media_versions (media_item_id, position, resolution, video_codec,"
        " bitrate_kbps, size_bytes, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (media_item_id, position, resolution, video_codec, bitrate_kbps, size_bytes,
         file_path))
    conn.commit()


def keep_forever(conn, media_item_id):
    conn.execute(
        "INSERT INTO protections (kind, media_item_id, detail) VALUES ('keep_forever', ?, 'movie')",
        (media_item_id,))
    conn.commit()


def curate(app, *keys):
    save_settings(replace(app.config['SETTINGS'], curated_libraries=tuple(keys)))


def two_library_copy(conn):
    """Heat in "Movies" and again in "Movies 4K": the same TMDB id."""
    movies = seed_library_row(conn, "1", "Movies")
    uhd = seed_library_row(conn, "2", "Movies 4K")
    hd_item = seed_movie(conn, movies, "11", "Heat", tmdb_id=949, size=12 * GB)
    uhd_item = seed_movie(conn, uhd, "12", "Heat", tmdb_id=949, size=40 * GB)
    seed_version(conn, hd_item, 0, resolution="1080", video_codec="h264",
                 bitrate_kbps=8_000, size_bytes=12 * GB)
    seed_version(conn, uhd_item, 0, resolution="4k", video_codec="hevc",
                 bitrate_kbps=25_000, size_bytes=40 * GB)
    return hd_item, uhd_item


def test_the_same_title_in_two_libraries_is_one_group_with_both_copies(client, conn):
    two_library_copy(conn)

    response = client.get("/duplicates?lang=en")

    assert response.status_code == 200
    text = page_text(response)
    assert translate("nav.duplicates", "en") in text
    assert text.count("Heat") == 1
    for library in ("Movies", "Movies 4K"):
        assert library in text
    for fact in ("1080", "h264", "4k", "hevc"):
        assert fact in text


def test_the_same_title_twice_in_one_library_is_not_a_duplicate(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    seed_movie(conn, movies, "11", "Heat", tmdb_id=949)
    seed_movie(conn, movies, "12", "Heat (1995)", tmdb_id=949)

    response = client.get("/duplicates?lang=en")

    assert translate("empty.duplicates", "en") in page_text(response)


def test_an_item_with_two_versions_shows_every_version(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    item = seed_movie(conn, movies, "11", "Heat", tmdb_id=949)
    seed_version(conn, item, 0, resolution="4k", video_codec="hevc",
                 bitrate_kbps=25_000, size_bytes=40 * GB)
    seed_version(conn, item, 1, resolution="1080", video_codec="h264",
                 bitrate_kbps=8_000, size_bytes=12 * GB)

    text = page_text(client.get("/duplicates?lang=en"))

    assert text.count("Heat") == 1
    for fact in ("4k", "hevc", "1080", "h264"):
        assert fact in text
    assert "40 GB" in text and "12 GB" in text
    assert "25000" in text and "8000" in text


def test_an_item_with_one_version_is_not_a_duplicate(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    item = seed_movie(conn, movies, "11", "Heat", tmdb_id=949)
    seed_version(conn, item, 0, resolution="1080", video_codec="h264")

    assert translate("empty.duplicates", "en") in page_text(
        client.get("/duplicates?lang=en"))


def test_a_keep_forever_copy_stays_and_says_so(client, conn):
    _, uhd_item = two_library_copy(conn)
    keep_forever(conn, uhd_item)

    text = page_text(client.get("/duplicates?lang=en"))

    assert "Movies 4K" in text
    assert translate("duplicates.marker.keep_forever", "en") in text


def test_a_protected_library_copy_stays_and_says_so(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    vault = seed_library_row(conn, "2", "The Vault", protected=True)
    seed_movie(conn, movies, "11", "Heat", tmdb_id=949)
    seed_movie(conn, vault, "12", "Heat", tmdb_id=949)

    text = page_text(client.get("/duplicates?lang=en"))

    assert "The Vault" in text
    assert translate("duplicates.marker.protected_library", "en") in text


def test_a_deselected_library_is_out_of_scope(client, conn, app):
    two_library_copy(conn)
    curate(app, "1")

    response = client.get("/duplicates?lang=en")

    text = page_text(response)
    assert response.status_code == 200
    assert "Movies 4K" not in text
    assert translate("empty.duplicates", "en") in text


def test_a_proven_absent_copy_is_out_of_scope(client, conn):
    hd_item, _ = two_library_copy(conn)
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?", (hd_item,))
    conn.commit()

    assert translate("empty.duplicates", "en") in page_text(
        client.get("/duplicates?lang=en"))


def test_an_empty_store_says_so(client, conn):
    response = client.get("/duplicates?lang=en")

    assert response.status_code == 200
    assert translate("empty.duplicates", "en") in page_text(response)


def test_a_fact_plex_never_sent_reads_unknown_never_zero(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    item = seed_movie(conn, movies, "11", "Heat", tmdb_id=949)
    seed_version(conn, item, 0)
    seed_version(conn, item, 1, resolution="1080", video_codec="h264")

    text = page_text(client.get("/duplicates?lang=en"))

    # Every fact Plex never sent is unknown on its copy, never a zero.
    assert translate("item.unknown", "en") in text
    assert "0 B" not in text


def test_the_screen_offers_no_action_and_changes_no_freed_space(client, conn, app):
    two_library_copy(conn)
    settings = replace(app.config['SETTINGS'], scheduler_enabled=False)
    before = summary_view(conn, settings)
    before_counts = [tuple(row) for row in conn.execute(
        "SELECT (SELECT COUNT(*) FROM protections), (SELECT COUNT(*) FROM candidates),"
        " (SELECT COUNT(*) FROM removal_requests), (SELECT COUNT(*) FROM removal_events)")]

    response = client.get("/duplicates?lang=en")

    text = page_text(response)
    assert translate("duplicates.read_only", "en") in text
    for action in ("action.mark", "action.keep_forever", "action.delete"):
        assert translate(action, "en") not in text
    assert "/removal" not in response.get_data(as_text=True)
    assert [tuple(row) for row in conn.execute(
        "SELECT (SELECT COUNT(*) FROM protections), (SELECT COUNT(*) FROM candidates),"
        " (SELECT COUNT(*) FROM removal_requests), (SELECT COUNT(*) FROM removal_events)"
    )] == before_counts
    after = summary_view(conn, settings)
    assert (after.candidates, after.leaving_soon, after.protected) == (
        before.candidates, before.leaving_soon, before.protected)


@pytest.mark.parametrize("locale", ["en", "ca", "es"])
def test_the_screen_is_translated_in_every_locale(client, conn, locale):
    two_library_copy(conn)

    text = page_text(client.get(f"/duplicates?lang={locale}"))

    for key in ("nav.duplicates", "duplicates.same_title", "duplicates.versions",
                "duplicates.read_only", "empty.duplicates"):
        assert translate(key, locale) in text or (
            key == "empty.duplicates" and translate("duplicates.read_only", locale) in text)


def test_the_navigation_links_to_the_duplicates_screen(client, conn):
    html = client.get("/?lang=en").get_data(as_text=True)

    assert 'href="/duplicates?lang=en"' in html
    assert translate("nav.duplicates", "en") in page_text(
        client.get("/?lang=en"))
    assert translate("nav.duplicates", "en") in page_text(client.get("/duplicates?lang=en"))


def two_version_item(conn, rating_key, title, *, tmdb_id=None):
    movies = conn.execute("SELECT id FROM libraries WHERE plex_key = '1'").fetchone()[0]
    item = seed_movie(conn, movies, rating_key, title, tmdb_id=tmdb_id)
    seed_version(conn, item, 0, resolution="4k", video_codec="hevc", size_bytes=40 * GB)
    seed_version(conn, item, 1, resolution="1080", video_codec="h264", size_bytes=12 * GB)
    return item


def test_the_copies_are_matched_by_an_imdb_id_too(client, conn):
    movies = seed_library_row(conn, "1", "Movies")
    uhd = seed_library_row(conn, "2", "Movies 4K")
    seed_movie(conn, movies, "11", "Heat", imdb_id="tt0113277")
    seed_movie(conn, uhd, "12", "Heat", imdb_id="tt0113277")

    text = page_text(client.get("/duplicates?lang=en"))

    assert "Movies 4K" in text and text.count("Heat") == 1


def test_a_series_is_matched_by_its_tvdb_id(client, conn):
    shows = seed_library_row(conn, "1", "TV Shows")
    archive = seed_library_row(conn, "2", "TV Archive")
    seed_movie(conn, shows, "200", "Lost in Space", tvdb_id=1234, item_type="series")
    seed_movie(conn, archive, "201", "Lost in Space", tvdb_id=1234, item_type="series")

    text = page_text(client.get("/duplicates?lang=en"))

    assert "TV Archive" in text and text.count("Lost in Space") == 1


def test_the_largest_group_is_shown_first_and_the_list_says_when_it_cuts(conn):
    from almali_library_curator.web.views import duplicates_view

    seed_library_row(conn, "1", "Movies")
    two_version_item(conn, "11", "Small", tmdb_id=1)
    big = two_version_item(conn, "12", "Big", tmdb_id=2)
    seed_version(conn, big, 2, resolution="4k", video_codec="hevc", size_bytes=90 * GB)

    view = duplicates_view(conn, None, limit=1)

    assert [group.title for group in view.versions] == ["Big"]
    assert view.truncated
    assert not view.empty


def test_the_versions_a_sync_stored_reach_the_screen(conn):
    """The whole path: Plex answers two versions, the sync stores them, the
    screen shows them. No internal table is read here."""
    dataset = FakePlexDataset()
    dataset.items["1"] = [two_version_movie()]
    with FakePlexServer(dataset) as server:
        PlexSync(PlexClient(base_url=server.base_url, token="test-token",
                            page_size=100, rate_limit_per_second=1000.0,
                            backoff_seconds=0.0), conn).run()

    [group] = duplicates_view(conn, None).versions

    assert group.title == "Heat"
    assert [(copy.resolution, copy.video_codec, copy.bitrate_kbps, copy.size_bytes)
            for copy in group.copies] == [
        ("4k", "hevc", 25_000, 40 * 1024**3),
        ("1080", "h264", 8_000, 12 * 1024**3),
    ]
