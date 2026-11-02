"""Shared fixtures for the detection tests.

Every scenario goes through the public interface: a fake Plex server is
synced into a real SQLite database with ``PlexSync``, and the engine runs
over that synced database.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The fake Plex server lives with the plex tests; reuse it without mocks.
PLEX_TESTS_DIR = Path(__file__).resolve().parent.parent / "plex"
if str(PLEX_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(PLEX_TESTS_DIR))


@pytest.fixture()
def conn(tmp_path):
    from library_curator.db import init_db

    connection = init_db(tmp_path / "detection.db")
    yield connection
    connection.close()
