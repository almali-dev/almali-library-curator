"""The sync reads its lookups once and writes its rows in batches (#10).

A full sync looked the season of every episode and the user of every play
event up, and wrote every row on its own: about 52,000 single-row statements
for a large library. The maps are read once and the rows are written in one
batch per table; the stored rows stay the same.
"""

from __future__ import annotations

import sqlite3

import pytest

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    episode,
    history_entry,
    movie,
    season,
    series,
)

from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync


class RecordingConnection:
    """A connection that counts how the sync writes its rows."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.single_row_writes = 0
        self.batched_writes = 0

    def execute(self, statement, parameters=()):
        if statement.lstrip().upper().startswith("INSERT"):
            self.single_row_writes += 1
        return self._connection.execute(statement, parameters)

    def executemany(self, statement, parameters):
        self.batched_writes += 1
        return self._connection.executemany(statement, parameters)

    def commit(self):
        return self._connection.commit()

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture()
def dataset() -> FakePlexDataset:
    """Three series with three seasons of four episodes, and twenty plays."""
    data = FakePlexDataset()
    data.items["1"] = [movie("100", "Tenet"), movie("101", "The Gray Man")]
    shows = []
    for show_number in range(3):
        series_key = f"2{show_number}"
        shows.append(series(series_key, f"Show {show_number}"))
        seasons = []
        for season_number in range(1, 4):
            season_key = f"{series_key}{season_number}"
            seasons.append(season(season_key, series_key, season_number))
            data.children[season_key] = [
                episode(f"{season_key}0{episode_number}", series_key, season_key, episode_number)
                for episode_number in range(1, 5)
            ]
        data.children[series_key] = seasons
    data.items["2"] = shows
    data.history = [
        history_entry(str(200 + number), 1 + number % 2, 1_700_000_000 + number,
                      6_000_000, 6_000_000)
        for number in range(20)
    ]
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
    return PlexClient(
        base_url=server.base_url,
        token="test-token",
        page_size=100,
        rate_limit_per_second=1000.0,
        backoff_seconds=0.0,
    )


def test_the_sync_reads_its_lookups_once_and_writes_its_rows_in_batches(
    server, conn
):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    recorder = RecordingConnection(conn)

    summary = PlexSync(build_client(server), recorder).run()

    assert (summary.media_items, summary.episodes, summary.play_events) == (14, 36, 20)

    # The old sync looked the season of every episode and the user of every
    # play event up: 58 reads for this library, plus one write per row.
    reads = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(reads) <= 5
    assert len(statements) <= 100

    # Every table is written in one batch.
    assert recorder.single_row_writes == 0
    assert recorder.batched_writes == 5
