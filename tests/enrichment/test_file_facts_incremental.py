"""#58: a refresh reads only what changed and resolves each directory's disk once.

The stored facts answer for a row whose Plex path and Plex-reported size are
unchanged since they were recorded, so a steady library costs no ``stat`` at
all. When a file must be read, the files of one directory share the disk that
the first of them resolved: Unraid keeps a share directory on one disk, so the
last-known disk is probed first and the other roots only when it misses.

Every probe is counted through the adapter's injected ``stat``: the tests never
reach the real disk for a file they did not ask about.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from almali_library_curator.enrichment import refresh_files
from almali_library_curator.hostfs import HostFilesystem

MOVIES = Path("Media/Movies")
SHOWS = Path("Media/Shows/Season 01")
DISKS = ("disk1", "disk2", "cache")

FIRST_MOVIE = b"first-movie"
SECOND_MOVIE = b"second"
PILOT = b"pilot"


class Probe:
    """The adapter's ``stat``, recording every path it is asked to read."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(os.fspath(path))
        return os.stat(path)

    def of(self, name: str) -> list[str]:
        """Every probe of the file called ``name``, in the order it happened."""
        return [call for call in self.calls if call.endswith(name)]

    def reset(self) -> None:
        self.calls.clear()


class Array:
    """A miniature Unraid array and the container's ``/media`` view of it."""

    def __init__(self, root: Path) -> None:
        self.media = root / "media"
        self.unraid = root / "unraid"
        self.media.mkdir()
        for disk in DISKS:
            (self.unraid / disk).mkdir(parents=True)
        self.probe = Probe()

    def file(self, relative: Path, payload: bytes, disk: str | None) -> Path:
        """One file in the media view, with its copy on ``disk`` (or nowhere)."""
        seen = self.media / relative
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.write_bytes(payload)
        if disk is not None:
            stored = self.unraid / disk / relative
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(payload)
        return seen

    def media_path(self, relative: Path) -> str:
        return str(self.media / relative)

    def disk_path(self, disk: str, relative: Path) -> str:
        return str(self.unraid / disk / relative)

    def host(self) -> HostFilesystem:
        return HostFilesystem(self.unraid, self.media, stat=self.probe)


def stored(conn, table: str, key: str):
    """The file facts one row currently carries."""
    return tuple(conn.execute(
        f"SELECT file_size_bytes, hardlink_count, disk FROM {table} WHERE plex_rating_key=?",
        (key,)).fetchone())


@pytest.fixture()
def library(conn, tmp_path):
    """Two movies sharing one directory on the cache pool, one episode on disk2."""
    array = Array(tmp_path)
    array.file(MOVIES / "first.mkv", FIRST_MOVIE, "cache")
    array.file(MOVIES / "second.mkv", SECOND_MOVIE, "cache")
    array.file(SHOWS / "pilot.mkv", PILOT, "disk2")
    conn.executemany(
        "INSERT INTO media_items (plex_rating_key, title, item_type, file_path,"
        " file_size_bytes) VALUES (?, ?, 'movie', ?, ?)",
        [("1", "First", array.media_path(MOVIES / "first.mkv"), len(FIRST_MOVIE)),
         ("2", "Second", array.media_path(MOVIES / "second.mkv"), len(SECOND_MOVIE))])
    conn.execute(
        "INSERT INTO episodes (plex_rating_key, series_rating_key, season_number,"
        " episode_number, title, file_path, file_size_bytes)"
        " VALUES ('10', '1', 1, 1, 'Pilot', ?, ?)",
        (array.media_path(SHOWS / "pilot.mkv"), len(PILOT)))
    conn.commit()
    array.conn = conn
    return array


def report_to_plex(conn, table: str, key: str, path: str, size: int) -> None:
    """What a sync writes before every refresh: the path and size Plex reports."""
    conn.execute(f"UPDATE {table} SET file_path=?, file_size_bytes=?"
                 " WHERE plex_rating_key=?", (path, size, key))
    conn.commit()


def test_a_second_refresh_with_unchanged_plex_data_probes_no_file(library):
    first = refresh_files(library.conn, library.host())
    assert (first.movies, first.episodes, first.unavailable) == (2, 1, 0)
    assert stored(library.conn, "media_items", "1") == (len(FIRST_MOVIE), 1, "cache")
    assert stored(library.conn, "episodes", "10") == (len(PILOT), 1, "disk2")

    library.probe.reset()
    second = refresh_files(library.conn, library.host())

    # Not one stat: not the media path, not a disk root.
    assert library.probe.calls == []
    assert (second.movies, second.episodes, second.unavailable) == (2, 1, 0)
    # The facts recorded last time are still the answer.
    assert stored(library.conn, "media_items", "1") == (len(FIRST_MOVIE), 1, "cache")
    assert stored(library.conn, "episodes", "10") == (len(PILOT), 1, "disk2")


def test_a_changed_size_or_path_reprobes_that_row_only(library):
    refresh_files(library.conn, library.host())
    library.probe.reset()

    # The first movie grew and Plex reports the new size for that row only.
    grown = FIRST_MOVIE + b"-grown"
    for path in (library.media / MOVIES / "first.mkv",
                 library.unraid / "cache" / MOVIES / "first.mkv"):
        path.write_bytes(grown)
    report_to_plex(library.conn, "media_items", "1",
                   library.media_path(MOVIES / "first.mkv"), len(grown))

    refresh_files(library.conn, library.host())

    assert library.probe.of("first.mkv")  # the changed row is read again
    assert not library.probe.of("second.mkv")  # unchanged rows are not
    assert not library.probe.of("pilot.mkv")
    assert stored(library.conn, "media_items", "1") == (len(grown), 1, "cache")

    # Plex renamed the second movie: only that row is read again.
    library.probe.reset()
    renamed = MOVIES / "second-renamed.mkv"
    (library.media / MOVIES / "second.mkv").rename(library.media / renamed)
    (library.unraid / "cache" / MOVIES / "second.mkv").rename(
        library.unraid / "cache" / renamed)
    report_to_plex(library.conn, "media_items", "2", library.media_path(renamed),
                   len(SECOND_MOVIE))

    refresh_files(library.conn, library.host())

    assert library.probe.of("second-renamed.mkv")
    assert not library.probe.of("first.mkv")
    assert not library.probe.of("pilot.mkv")
    assert stored(library.conn, "media_items", "2") == (len(SECOND_MOVIE), 1, "cache")


def test_files_in_one_directory_probe_the_disk_once(library):
    refresh_files(library.conn, library.host())

    # The first movie of the directory resolves it: every root is tried in
    # order, and the cache pool is the last one.
    assert library.probe.of("first.mkv") == [
        library.media_path(MOVIES / "first.mkv"),
        library.disk_path("disk1", MOVIES / "first.mkv"),
        library.disk_path("disk2", MOVIES / "first.mkv"),
        library.disk_path("cache", MOVIES / "first.mkv"),
    ]
    # The directory is known now: the next movie asks the cache pool only.
    assert library.probe.of("second.mkv") == [
        library.media_path(MOVIES / "second.mkv"),
        library.disk_path("cache", MOVIES / "second.mkv"),
    ]


def test_the_last_known_disk_is_probed_first_and_a_miss_still_searches(conn, tmp_path):
    array = Array(tmp_path)
    learned = array.file(MOVIES / "learned.mkv", b"learned", "cache")
    ghost = array.file(MOVIES / "ghost.mkv", b"ghost", None)  # no disk claims it
    moved = array.file(MOVIES / "moved.mkv", b"moved", "disk2")  # same directory
    # The refresh reads the rows by id, so the disk is learned from the first.
    conn.executemany(
        "INSERT INTO media_items (plex_rating_key, title, item_type, file_path,"
        " file_size_bytes) VALUES (?, ?, 'movie', ?, ?)",
        [("1", "Learned", str(learned), len(b"learned")),
         ("2", "Ghost", str(ghost), len(b"ghost")),
         ("3", "Moved", str(moved), len(b"moved"))])
    conn.commit()

    refresh_files(conn, array.host())

    assert array.probe.of("learned.mkv") == [
        str(learned),
        array.disk_path("disk1", MOVIES / "learned.mkv"),
        array.disk_path("disk2", MOVIES / "learned.mkv"),
        array.disk_path("cache", MOVIES / "learned.mkv"),
    ]
    # The learned pool goes first and its miss still searches every other root.
    assert array.probe.of("ghost.mkv") == [
        str(ghost),
        array.disk_path("cache", MOVIES / "ghost.mkv"),
        array.disk_path("disk1", MOVIES / "ghost.mkv"),
        array.disk_path("disk2", MOVIES / "ghost.mkv"),
    ]
    # The ghost's "no disk" answer is not remembered for its siblings.
    assert array.probe.of("moved.mkv") == [
        str(moved),
        array.disk_path("cache", MOVIES / "moved.mkv"),
        array.disk_path("disk1", MOVIES / "moved.mkv"),
        array.disk_path("disk2", MOVIES / "moved.mkv"),
    ]
    assert stored(conn, "media_items", "3") == (len(b"moved"), 1, "disk2")


def test_a_new_disk_layout_makes_the_recorded_facts_doubtful(conn, tmp_path):
    # The array is not mounted yet: the refresh records sizes but no disks.
    array = Array(tmp_path)
    for disk in DISKS:
        (array.unraid / disk).rmdir()
    array.unraid.rmdir()
    movie = array.file(MOVIES / "movie.mkv", b"movie", None)
    conn.execute(
        "INSERT INTO media_items (plex_rating_key, title, item_type, file_path,"
        " file_size_bytes) VALUES ('1', 'Movie', 'movie', ?, ?)",
        (str(movie), len(b"movie")))
    conn.commit()
    refresh_files(conn, array.host())
    assert stored(conn, "media_items", "1") == (len(b"movie"), 1, None)

    # The mount appears. Plex says nothing new, but the recorded answer was
    # written without the disks, so the row is read again and gains its disk.
    (array.unraid / "disk1").mkdir(parents=True)
    (array.unraid / "disk1" / MOVIES).mkdir(parents=True)
    (array.unraid / "disk1" / MOVIES / "movie.mkv").write_bytes(b"movie")
    array.probe.reset()

    refresh_files(conn, array.host())

    assert array.probe.of("movie.mkv")  # its recorded answer is doubted
    assert stored(conn, "media_items", "1") == (len(b"movie"), 1, "disk1")


def test_a_file_that_disappeared_is_cleared_when_its_plex_data_changed(library):
    refresh_files(library.conn, library.host())
    library.probe.reset()

    # The file changed on disk (Plex reports a new size) and is gone by the
    # time the refresh runs: the row is read like today and its facts cleared.
    (library.media / MOVIES / "first.mkv").unlink()
    report_to_plex(library.conn, "media_items", "1",
                   library.media_path(MOVIES / "first.mkv"), 999)

    summary = refresh_files(library.conn, library.host())

    assert summary.unavailable == 1
    assert library.probe.of("first.mkv") == [library.media_path(MOVIES / "first.mkv")]
    assert stored(library.conn, "media_items", "1") == (999, None, None)
    # The unchanged rows are still skipped in the same pass.
    assert not library.probe.of("second.mkv")
    assert stored(library.conn, "media_items", "2") == (len(SECOND_MOVIE), 1, "cache")
