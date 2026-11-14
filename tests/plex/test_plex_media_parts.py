"""The parser reads files and sizes from ``Media[].Part[]`` (S1, #37).

Verified on 2026-11-13 against the owner's live server: the payload of
``GET /library/sections/<key>/all?type=1`` carries no top-level ``Part``.
The file lives in ``Media[].Part[]``, and a ``Part`` entry carries the keys
``container, duration, file, id, indexes, key, size, videoProfile``.
"""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from almali_library_curator.plex import PlexClient
from almali_library_curator.plex.models import PlexMetadata


def live_part(file_: str, size: int, **extra) -> dict:
    """One ``Part`` entry with the exact keys the live server answers."""
    data = {
        "container": "MKV",
        "duration": 2_700_000,
        "file": file_,
        "id": 12345,
        "indexes": "sd",
        "key": "/library/parts/12345/file.mkv",
        "size": size,
        "videoProfile": "high",
    }
    data.update(extra)
    return data


def live_movie(rating_key: str, title: str, media: list[dict], **extra) -> dict:
    """A movie metadata entry mirroring the live payload shape."""
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "movie",
        "title": title,
        "addedAt": 1711929600,
        "Media": media,
    }
    data.update(extra)
    return data


def test_file_path_and_size_are_read_from_media_parts():
    payload = live_movie(
        "11",
        "Heat",
        [{"id": 1, "videoProfile": "high", "Part": [
            live_part("/media/Movies/Heat/Heat.mkv", 24 * 1024**3)
        ]}],
    )

    item = PlexMetadata(**payload)

    assert item.file_path == "/media/Movies/Heat/Heat.mkv"
    assert item.file_size == 24 * 1024**3


def test_several_media_entries_pick_the_first_one_in_document_order():
    # Two Media entries are two versions of the same movie (for example a
    # 4K and a 1080p file). The primary file is the first Media entry's
    # part, in the order the server sent them.
    payload = live_movie(
        "11",
        "Heat",
        [
            {"id": 1, "videoProfile": "high", "Part": [
                live_part("/media/Movies/Heat/Heat - 2160p.mkv", 40 * 1024**3)
            ]},
            {"id": 2, "videoProfile": "main", "Part": [
                live_part("/media/Movies/Heat/Heat - 1080p.mkv", 12 * 1024**3)
            ]},
        ],
    )

    item = PlexMetadata(**payload)

    assert item.file_path == "/media/Movies/Heat/Heat - 2160p.mkv"
    assert item.file_size == 40 * 1024**3
    assert len(item.all_parts) == 2


def test_several_parts_in_one_media_pick_the_first_one_in_document_order():
    # A stacked movie (for example a two-disc film) carries several Part
    # entries inside one Media. The primary file is the first part.
    payload = live_movie(
        "12",
        "Kill Bill",
        [{"id": 1, "videoProfile": "high", "Part": [
            live_part("/media/Movies/Kill Bill/Kill Bill - Disc 1.mkv", 20 * 1024**3),
            live_part("/media/Movies/Kill Bill/Kill Bill - Disc 2.mkv", 19 * 1024**3),
        ]}],
    )

    item = PlexMetadata(**payload)

    assert item.file_path == "/media/Movies/Kill Bill/Kill Bill - Disc 1.mkv"
    assert item.file_size == 20 * 1024**3
    assert len(item.all_parts) == 2


def test_a_top_level_part_still_works_as_a_fallback():
    # Older payload shapes keep a top-level Part. It stays a documented
    # fallback, so those servers keep working.
    item = PlexMetadata(
        ratingKey="11",
        title="Heat",
        Part=[{"file": "/media/Movies/Heat/Heat.mkv", "size": 24 * 1024**3}],
    )

    assert item.file_path == "/media/Movies/Heat/Heat.mkv"
    assert item.file_size == 24 * 1024**3
    assert len(item.all_parts) == 1


def test_the_fake_server_serves_the_real_shape_over_the_wire():
    # The fake Plex server must serve the shape the live server really
    # has, so the client is exercised against the real contract.
    dataset = FakePlexDataset()
    dataset.items["1"] = [
        movie("11", "Heat"),
        movie(
            "12",
            "Heat",
            Media=[
                {"id": 1, "videoProfile": "high", "Part": [
                    live_part("/media/Movies/Heat/Heat - 2160p.mkv", 40 * 1024**3)
                ]},
                {"id": 2, "videoProfile": "main", "Part": [
                    live_part("/media/Movies/Heat/Heat - 1080p.mkv", 12 * 1024**3)
                ]},
            ],
        ),
    ]

    with FakePlexServer(dataset) as server:
        client = PlexClient(
            base_url=server.base_url,
            token="test-token",
            page_size=100,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        items = client.section_items("1", "movie")

    assert items[0].Media[0].Part[0].file == "/media/Movies/Heat/Heat.mkv"
    assert not items[0].Part  # the real shape carries no top-level Part
    assert items[1].file_path == "/media/Movies/Heat/Heat - 2160p.mkv"
    assert items[1].file_size == 40 * 1024**3
