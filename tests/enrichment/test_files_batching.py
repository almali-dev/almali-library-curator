"""File refresh batches writes without changing the stored file facts."""

from __future__ import annotations

import sqlite3

import pytest

from almali_library_curator.enrichment import refresh_files
from almali_library_curator.hostfs import FileFacts


class StubHostFilesystem:
    def __init__(self, facts: dict[str, FileFacts | None]) -> None:
        self.facts = facts
        self.stat_calls: list[str] = []

    def file_facts(self, path: str):
        self.stat_calls.append(path)
        return self.facts.get(path)


def _insert_movies(conn, paths: list[str]) -> None:
    conn.executemany(
        "INSERT INTO media_items (plex_rating_key, title, item_type, file_path) "
        "VALUES (?, ?, 'movie', ?)",
        [(str(index + 1), f"Movie {index}", path) for index, path in enumerate(paths)],
    )
    conn.commit()


def test_refresh_files_batches_updates_without_changing_file_facts(conn):
    movie_paths = [f"/media/movie-{index}.mkv" for index in range(1000)]
    episode_path = "/media/episode.mkv"
    _insert_movies(conn, movie_paths)
    conn.execute(
        "INSERT INTO episodes (media_item_id, plex_rating_key, series_rating_key, file_path) "
        "VALUES (1, 'episode-1', 'series-1', ?)",
        (episode_path,),
    )
    conn.commit()

    facts = {
        path: FileFacts(size_bytes=11 + index, hardlink_count=2, disk="disk3")
        for index, path in enumerate(movie_paths)
    }
    facts[episode_path] = FileFacts(size_bytes=11, hardlink_count=2, disk="disk3")
    host = StubHostFilesystem(facts)

    statements = []
    conn.set_trace_callback(statements.append)
    summary = refresh_files(conn, host)
    conn.set_trace_callback(None)

    updates = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("UPDATE ")
    ]
    assert len(updates) == 2
    assert len(host.stat_calls) == 1001
    assert (summary.movies, summary.episodes, summary.unavailable) == (1000, 1, 0)

    movie_facts = conn.execute(
        "SELECT count(*), min(file_size_bytes), max(file_size_bytes), "
        "min(hardlink_count), max(hardlink_count), count(disk) FROM media_items"
    ).fetchone()
    assert tuple(movie_facts) == (1000, 11, 1010, 2, 2, 1000)
    episode_facts = conn.execute(
        "SELECT count(*), min(file_size_bytes), max(file_size_bytes), "
        "min(hardlink_count), max(hardlink_count), count(disk) FROM episodes"
    ).fetchone()
    assert tuple(episode_facts) == (1, 11, 11, 2, 2, 1)


def test_refresh_files_keeps_the_existing_size_when_inspection_is_unavailable(conn):
    conn.execute(
        "INSERT INTO media_items (plex_rating_key, title, item_type, file_path, "
        "file_size_bytes) VALUES ('1', 'Movie', 'movie', '/media/missing.mkv', 999)"
    )
    conn.commit()
    host = StubHostFilesystem({"/media/missing.mkv": None})

    summary = refresh_files(conn, host)

    assert (summary.movies, summary.episodes, summary.unavailable) == (1, 0, 1)
    row = conn.execute(
        "SELECT file_size_bytes, hardlink_count, disk FROM media_items WHERE id = 1"
    ).fetchone()
    assert tuple(row) == (999, None, None)


def test_refresh_files_rolls_back_the_whole_batch_when_one_update_fails(conn):
    paths = ["/media/one.mkv", "/media/two.mkv", "/media/three.mkv"]
    _insert_movies(conn, paths)
    conn.execute(
        "CREATE TRIGGER fail_second_file_update BEFORE UPDATE ON media_items "
        "WHEN NEW.plex_rating_key = '2' BEGIN SELECT RAISE(ABORT, 'update failed'); END"
    )
    conn.commit()
    host = StubHostFilesystem(
        {path: FileFacts(size_bytes=10, hardlink_count=1, disk="disk1") for path in paths}
    )

    with pytest.raises(sqlite3.DatabaseError):
        refresh_files(conn, host)

    rows = conn.execute(
        "SELECT file_size_bytes, hardlink_count, disk FROM media_items ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(None, None, None)] * 3
