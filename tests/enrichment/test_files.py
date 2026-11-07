import os

import pytest

from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.plex import PlexClient, PlexSync
from fake_plex_server import FakePlexDataset, FakePlexServer, episode, movie, season, series


def sync_files(conn, movie_path, episode_path):
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Movie", Part=[{"file": str(movie_path), "size": 999}])]
    data.items["2"] = [series("200", "Show")]
    data.children["200"] = [season("210", "200", 1)]
    data.children["210"] = [episode("300", "200", "210", 1,
                                    Part=[{"file": str(episode_path), "size": 888}])]
    with FakePlexServer(data) as server:
        PlexSync(PlexClient(server.base_url, "test-token", rate_limit_per_second=0), conn).run()


def test_refresh_files_records_real_sizes_disks_and_hardlinks(conn, tmp_path):
    from almali_library_curator.enrichment import refresh_files
    media = tmp_path / "media"
    disk = tmp_path / "mnt" / "disk3"
    media.mkdir()
    disk.mkdir(parents=True)
    movie_path = media / "movie.mkv"
    movie_path.write_bytes(b"movie-data")
    os.link(movie_path, media / "outside-link.mkv")
    episode_path = media / "episode.mkv"
    episode_path.write_bytes(b"episode")
    (disk / "movie.mkv").write_bytes(b"movie-data")
    (disk / "episode.mkv").write_bytes(b"episode")
    sync_files(conn, movie_path, episode_path)
    summary = refresh_files(conn, HostFilesystem(tmp_path / "mnt", media))
    assert (summary.movies, summary.episodes, summary.unavailable) == (1, 1, 0)
    for table, key, expected in [("media_items", "100", (10, 2, "disk3")),
                                  ("episodes", "300", (7, 1, "disk3"))]:
        row = conn.execute(f"SELECT file_size_bytes, hardlink_count, disk FROM {table} "
                           "WHERE plex_rating_key=?", (key,)).fetchone()
        assert tuple(row) == expected


@pytest.mark.parametrize("mode", ["absent", "missing", "unreadable", "no_unraid"])
def test_refresh_files_degrades_without_inventing_file_facts(conn, tmp_path, mode):
    from almali_library_curator.enrichment import refresh_files
    path = tmp_path / "media" / "file.mkv"
    path.parent.mkdir()
    if mode != "missing":
        path.write_bytes(b"data")
    sync_files(conn, path, path)
    def denied(path):
        raise PermissionError("not readable")
    host = None if mode == "absent" else HostFilesystem(
        media_root=path.parent, **({"stat": denied} if mode == "unreadable" else {}))
    summary = refresh_files(conn, host)
    expected_link = 1 if mode == "no_unraid" else None
    assert summary.unavailable == (0 if mode == "no_unraid" else 2)
    for table, key in [("media_items", "100"), ("episodes", "300")]:
        row = conn.execute(f"SELECT disk, hardlink_count, file_size_bytes FROM {table} "
                           "WHERE plex_rating_key=?", (key,)).fetchone()
        assert (row["disk"], row["hardlink_count"]) == (None, expected_link)
        # Unavailable host inspection must not erase Plex's reported size (S2).
        assert row["file_size_bytes"] == (4 if mode == "no_unraid" else (999 if key == "100" else 888))
