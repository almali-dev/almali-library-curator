"""S2 (#132): the sync stores every version of an item.

The database keeps one row per Plex ``Media`` entry, so a later scan of the
same library neither duplicates nor loses them. The item's own
``file_path``/``file_size_bytes`` keep meaning what they always meant: the
first file of the first version.
"""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from test_plex_media_parts import live_movie, live_part
from test_plex_versions import live_media, two_version_movie

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync

GB = 1024**3
VERSION_COLUMNS = ("position, plex_media_id, video_profile, resolution,"
                   " video_codec, bitrate_kbps, size_bytes, file_path")


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items["1"] = [two_version_movie()]
    return data


@pytest.fixture()
def server(dataset):
    with FakePlexServer(dataset) as running:
        yield running


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "almali-library-curator.db")
    yield connection
    connection.close()


def build_client(server) -> PlexClient:
    return PlexClient(base_url=server.base_url, token="test-token", page_size=100,
                      rate_limit_per_second=1000.0, backoff_seconds=0.0)


def versions(conn):
    return [tuple(row) for row in conn.execute(
        f"SELECT {VERSION_COLUMNS} FROM media_versions ORDER BY position")]


def test_the_sync_stores_every_version_with_its_own_facts(server, conn):
    PlexSync(build_client(server), conn).run()

    assert versions(conn) == [
        (0, 31, "main", "4k", "hevc", 25_000, 40 * GB,
         "/media/Movies/Heat/Heat - 2160p.mkv"),
        (1, 32, "main", "1080", "h264", 8_000, 12 * GB,
         "/media/Movies/Heat/Heat - 1080p.mkv"),
    ]


def test_the_item_keeps_its_first_file_and_size(server, conn):
    """The behaviour the rest of the app reads (#37) still holds."""
    PlexSync(build_client(server), conn).run()

    row = conn.execute(
        "SELECT file_path, file_size_bytes FROM media_items WHERE plex_rating_key = '11'"
    ).fetchone()
    assert (row["file_path"], row["file_size_bytes"]) == (
        "/media/Movies/Heat/Heat - 2160p.mkv", 40 * GB)


def test_a_field_plex_omits_is_stored_as_null(server, conn, dataset):
    entry = live_media(31, "1080", "h264", 8_000, [live_part("/media/Heat.mkv", 1024)])
    del entry["bitrate"]
    del entry["videoResolution"]
    entry["Part"][0].pop("size")
    dataset.items["1"] = [live_movie("11", "Heat", [entry])]

    PlexSync(build_client(server), conn).run()

    [row] = conn.execute(
        "SELECT resolution, bitrate_kbps, size_bytes FROM media_versions").fetchall()
    assert tuple(row) == (None, None, None)


def test_a_second_sync_of_the_same_library_stores_nothing_new(server, conn):
    PlexSync(build_client(server), conn).run()
    PlexSync(build_client(server), conn).run()

    assert len(versions(conn)) == 2


def test_a_version_plex_no_longer_reports_leaves_the_store(server, conn, dataset):
    PlexSync(build_client(server), conn).run()

    dataset.items["1"] = [live_movie("11", "Heat", [
        live_media(32, "1080", "h264", 8_000,
                   [live_part("/media/Movies/Heat/Heat - 1080p.mkv", 12 * GB)]),
    ])]
    PlexSync(build_client(server), conn).run()

    assert versions(conn) == [
        (0, 32, "main", "1080", "h264", 8_000, 12 * GB,
         "/media/Movies/Heat/Heat - 1080p.mkv")]


def test_an_item_without_media_keeps_its_file_and_stores_no_version(server, conn, dataset):
    """The older payload shape still syncs and still shows its one file."""
    dataset.items["1"] = [{
        "ratingKey": "11", "type": "movie", "title": "Heat",
        "addedAt": 1711929600,
        "Part": [live_part("/media/Movies/Heat/Heat.mkv", 24 * GB)],
    }]

    PlexSync(build_client(server), conn).run()

    assert versions(conn) == []
    row = conn.execute(
        "SELECT file_path, file_size_bytes FROM media_items WHERE plex_rating_key = '11'"
    ).fetchone()
    assert (row["file_path"], row["file_size_bytes"]) == (
        "/media/Movies/Heat/Heat.mkv", 24 * GB)
