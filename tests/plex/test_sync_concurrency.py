"""A remote fetch must never reserve SQLite's writer slot."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from almali_library_curator.db import connect, init_db
from almali_library_curator.lifecycle.safety import kill_switch_enabled, set_kill_switch
from almali_library_curator.plex import PlexClient, PlexSync


@pytest.mark.parametrize('boundary', ['section_items', 'accounts', 'history'])
def test_kill_switch_commits_from_another_connection_while_sync_fetches(tmp_path, boundary):
    path = tmp_path / 'curator.db'
    init_db(path).close()
    entered, release = Event(), Event()
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Tenet')]
    with FakePlexServer(data) as server:
        client = PlexClient(server.base_url, 'test-token', rate_limit_per_second=0)
        original = getattr(client, boundary)
        def held(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        setattr(client, boundary, held)
        def sync():
            conn = connect(path)
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                summary = PlexSync(client, conn).run()
                assert [sql for sql in statements if sql.startswith('BEGIN')] == ['BEGIN IMMEDIATE']
                assert statements.count('COMMIT') == 1
                return summary
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sync)
            try:
                assert entered.wait(5)
                action = connect(path)
                try:
                    # Fail promptly if the sync holds the writer; don't wait for release.
                    action.execute('PRAGMA busy_timeout = 200')
                    set_kill_switch(action, True)
                    assert kill_switch_enabled(action)
                    assert not release.is_set() and not future.done()
                finally:
                    action.close()
            finally:
                release.set()
            assert future.result(timeout=5).media_items == 1
    check = connect(path)
    assert kill_switch_enabled(check)
    assert check.execute("SELECT title FROM media_items WHERE plex_rating_key='100'").fetchone()[0] == 'Tenet'
    check.close()


def test_failure_during_publication_rolls_back_the_whole_snapshot(tmp_path):
    import sqlite3
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Tenet')]
    conn = init_db(tmp_path / 'curator.db')
    set_kill_switch(conn, True)
    def deny_media_insert(action, table, column, database, source):
        if action == sqlite3.SQLITE_INSERT and table == 'media_items':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(deny_media_insert)
    with FakePlexServer(data) as server:
        client = PlexClient(server.base_url, 'test-token', rate_limit_per_second=0)
        with pytest.raises(sqlite3.DatabaseError):
            PlexSync(client, conn).run()
    conn.set_authorizer(None)
    assert not conn.in_transaction
    for table in ('libraries', 'media_items', 'episodes', 'users', 'play_events'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert kill_switch_enabled(conn)
    conn.close()


def test_a_pending_transaction_is_joined_and_never_committed_by_the_sync(tmp_path):
    """The caller keeps its transaction; the sync publishes inside it."""
    conn = init_db(tmp_path / 'curator.db')
    conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                 " VALUES ('1', 'Movies', 'movie')")
    assert conn.in_transaction
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Tenet')]
    with FakePlexServer(data) as server:
        client = PlexClient(server.base_url, 'test-token', rate_limit_per_second=0)
        summary = PlexSync(client, conn).run()
    assert summary.media_items == 1
    # The caller's transaction is still open and still owns its own work.
    assert conn.in_transaction
    conn.rollback()
    # Nothing was committed: neither the caller's row nor the snapshot.
    assert conn.execute('SELECT COUNT(*) FROM libraries').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 0
    conn.close()
