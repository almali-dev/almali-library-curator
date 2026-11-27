"""S1 (#145): one Plex client identifier per installation.

Every request the app makes to plex.tv and to Plex carries the installation's
own identifier: random, at least 128 bits, created once under the config
directory with mode 0600, and shared by the sign-in and by the Plex client.
A missing, unreadable or corrupt file never stops the app, and nothing else is
touched, so an existing installation keeps its stored token after the upgrade.

Everything goes over HTTP to the real fake servers in ``tests/``; no test
reads the identifier out of a request the app never sends.
"""
from __future__ import annotations

from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from fake_plex_tv_server import FakePlexTvDataset, FakePlexTvServer

from almali_library_curator import secret_store
from almali_library_curator.app import create_app
from almali_library_curator.config import load_settings
from almali_library_curator.plex import PlexClient, auth
from almali_library_curator.plex import identity

IDENTIFIER_FILE_NAME = 'plex_client_identifier'
IDENTIFIER_PATTERN = re.compile(r'[0-9a-f]{32}')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def dataset() -> FakePlexDataset:
    data = FakePlexDataset()
    data.items['1'] = [movie('100', 'Tenet')]
    return data


@pytest.fixture()
def server(dataset):
    with FakePlexServer(dataset) as running:
        yield running


@pytest.fixture()
def plex_tv():
    with FakePlexTvServer(FakePlexTvDataset()) as running:
        yield running


@pytest.fixture()
def config_dir(tmp_path) -> Path:
    return tmp_path / 'config'


def identifier_file(config_dir: Path) -> Path:
    return config_dir / IDENTIFIER_FILE_NAME


def build_client(config_dir: Path, server) -> PlexClient:
    """The client the app builds, pointed at the fake Plex server."""
    settings = replace(
        load_settings(config_dir, env={}),
        plex_base_url=server.base_url,
        plex_token='test-token',
        plex_rate_limit_per_second=1000.0,
    )
    return PlexClient.from_settings(settings, backoff_seconds=0.0)


def sent_identifier(server, index=0) -> str:
    return server.requests_for('/library/sections')[index]['headers']['X-Plex-Client-Identifier']


def warnings_named(caplog, name):
    return [record for record in caplog.records if record.getMessage() == name]


# -- rule 1: random, at least 128 bits, owner-only, one per installation ------


def test_the_plex_client_announces_a_random_owner_only_identifier(config_dir, server):
    build_client(config_dir, server).sections()

    path = identifier_file(config_dir)
    stored = path.read_text().strip()
    assert sent_identifier(server) == stored
    # At least 128 bits of randomness, stored as hex.
    assert IDENTIFIER_PATTERN.fullmatch(stored), stored
    assert len(stored) * 4 >= 128
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_identifier_is_created_once_and_reused_across_restarts(config_dir, server):
    first = build_client(config_dir, server)
    first.sections()
    path = identifier_file(config_dir)
    written = path.read_bytes()

    # A restart is a new process: read the same file again with no cache.
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys\n'
         'from almali_library_curator.plex import identity\n'
         'print(identity.client_identifier(sys.argv[1]))', str(config_dir)],
        cwd=ROOT, capture_output=True, text=True, check=True,
        env={**os.environ, 'PYTHONPATH': str(ROOT)},
    )

    assert result.stdout.strip() == first.client_identifier
    assert path.read_bytes() == written
    second = build_client(config_dir, server)
    second.sections()
    assert second.client_identifier == first.client_identifier
    assert path.read_bytes() == written


def test_two_installations_never_share_an_identifier(tmp_path, server):
    first = build_client(tmp_path / 'one', server)
    second = build_client(tmp_path / 'two', server)
    first.sections()
    assert sent_identifier(server) == first.client_identifier
    second.sections()

    assert first.client_identifier != second.client_identifier
    assert sent_identifier(server, -1) == second.client_identifier


def test_the_sign_in_and_the_plex_client_use_the_same_identifier(
        config_dir, server, plex_tv, monkeypatch, owner_client_class):
    """The PIN is created with the identifier the Plex client announces."""
    monkeypatch.setattr(auth, 'PLEX_TV_BASE_URL', plex_tv.base_url)
    monkeypatch.setattr(auth, 'DEFAULT_RATE_LIMIT_PER_SECOND', 10000.0)
    app = create_app(replace(load_settings(config_dir, env={}), scheduler_enabled=False))
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app.test_client_class = owner_client_class

    response = app.test_client().post('/connections/plex/signin', data={'origin': 'connections'})

    assert response.status_code == 303
    creation = plex_tv.pin_creations[0]
    client = build_client(config_dir, server)
    # The stored sign-in state carries the same identifier the link shows.
    state = json.loads((config_dir / 'plex-signin.json').read_text())
    assert creation['headers']['X-Plex-Client-Identifier'] == client.client_identifier
    assert state['client_identifier'] == client.client_identifier


# -- rule 2: a missing, unreadable or corrupt file never stops the app --------


def test_a_corrupt_identifier_file_is_replaced_with_a_warning(config_dir, server, caplog):
    config_dir.mkdir(parents=True)
    identifier_file(config_dir).write_text('not-an-identifier\n')
    caplog.set_level(logging.WARNING)

    client = build_client(config_dir, server)
    client.sections()

    stored = identifier_file(config_dir).read_text().strip()
    assert IDENTIFIER_PATTERN.fullmatch(stored)
    assert client.client_identifier == stored == sent_identifier(server)
    assert stat.S_IMODE(identifier_file(config_dir).stat().st_mode) == 0o600
    [replaced] = warnings_named(caplog, 'plex_client_identifier_replaced')
    assert replaced.reason == 'corrupt'


def test_a_directory_where_the_file_belongs_never_stops_the_app(config_dir, server, caplog):
    identifier_file(config_dir).mkdir(parents=True)
    caplog.set_level(logging.WARNING)

    client = build_client(config_dir, server)
    client.sections()

    # The request went out, with a fresh identifier, and the app kept running.
    assert IDENTIFIER_PATTERN.fullmatch(sent_identifier(server))
    assert client.client_identifier == sent_identifier(server)
    assert warnings_named(caplog, 'plex_client_identifier_replaced')


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores the file mode')
def test_an_unreadable_identifier_file_is_replaced_with_a_warning(config_dir, server, caplog):
    config_dir.mkdir(parents=True)
    identifier_file(config_dir).write_text('a' * 32)
    identifier_file(config_dir).chmod(0o000)
    caplog.set_level(logging.WARNING)

    client = build_client(config_dir, server)
    client.sections()

    [replaced] = warnings_named(caplog, 'plex_client_identifier_replaced')
    assert replaced.reason == 'unreadable'
    assert client.client_identifier == sent_identifier(server)
    assert IDENTIFIER_PATTERN.fullmatch(client.client_identifier)


def test_an_install_with_no_identifier_starts_with_one(settings, server):
    # A fresh install has no config directory yet; the first request creates it.
    client = build_client(settings.config_dir, server)
    client.sections()

    assert identifier_file(settings.config_dir).is_file()


# -- rule 3: nothing else is touched, so the stored token stays valid --------


def test_creating_the_identifier_touches_nothing_else(config_dir, server):
    config_dir.mkdir(parents=True)
    load_settings(config_dir, env={})                      # settings.json
    secret_store.store_plex_token(config_dir, 'stored-plex-token-1234')
    (config_dir / 'almali-library-curator.db').write_bytes(b'database-bytes')
    before = {
        path.relative_to(config_dir): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in config_dir.rglob('*') if path.is_file()
    }

    build_client(config_dir, server).sections()

    after = {
        path.relative_to(config_dir): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in config_dir.rglob('*')
        if path.is_file() and path.name not in (IDENTIFIER_FILE_NAME,
                                                '.plex_client_identifier.lock')
    }
    assert after == before
    # The stored token is still there and still the one the app reads.
    assert secret_store.read_plex_token(config_dir).token == 'stored-plex-token-1234'
    reloaded = load_settings(config_dir, env={})
    assert reloaded.plex_token == 'stored-plex-token-1234'
    assert load_settings(config_dir, env={}).plex_base_url == ''
