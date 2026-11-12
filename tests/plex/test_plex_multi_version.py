"""S3: one small client read that counts an item's distinct files.

The fake server serves the live multi-version shape (``Media`` entries
with their own ``Part`` arrays and distinct files) on the single-item
metadata endpoint, exactly as a live server answers it.
"""

from __future__ import annotations

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from almali_library_curator.plex import PlexClient

from test_plex_media_parts import live_movie, live_part


def client_for(server, **overrides):
    options = {"base_url": server.base_url, "token": "test-token",
               "rate_limit_per_second": 0, "backoff_seconds": 0.0, "max_retries": 0}
    options.update(overrides)
    return PlexClient(**options)


def two_version_movie(title: str) -> dict:
    return live_movie("11", title, [
        {"id": 1, "videoProfile": "high", "Part": [
            live_part(f"/media/Movies/{title}/{title} - 2160p.mkv", 40 * 1024**3)]},
        {"id": 2, "videoProfile": "main", "Part": [
            live_part(f"/media/Movies/{title}/{title} - 1080p.mkv", 12 * 1024**3)]},
    ])


def test_media_files_reads_every_distinct_file_of_a_multi_version_item():
    dataset = FakePlexDataset()
    dataset.metadata["11"] = two_version_movie("Heat")
    with FakePlexServer(dataset) as server:
        client = client_for(server)
        files = client.media_files("11")
    assert files == [
        "/media/Movies/Heat/Heat - 1080p.mkv",
        "/media/Movies/Heat/Heat - 2160p.mkv",
    ]


def test_media_files_is_independent_of_the_document_order():
    entry = two_version_movie("Heat")
    entry["Media"].reverse()  # The API proves no "first entry is primary" rule.
    dataset = FakePlexDataset()
    dataset.metadata["11"] = entry
    with FakePlexServer(dataset) as server:
        assert client_for(server).media_files("11") == [
            "/media/Movies/Heat/Heat - 1080p.mkv",
            "/media/Movies/Heat/Heat - 2160p.mkv",
        ]


def test_media_files_answers_one_file_for_a_single_version_item():
    dataset = FakePlexDataset()
    dataset.metadata["11"] = movie("11", "Heat")
    with FakePlexServer(dataset) as server:
        assert client_for(server).media_files("11") == [
            "/media/Movies/Heat/Heat.mkv"]
