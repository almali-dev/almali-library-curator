"""Shared fixtures for the lifecycle tests.

Every rule runs against a real migrated database (``init_db``), seeded
either directly or through the fake Plex server and ``PlexSync`` — never
through mocks of the app's own code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The fake Plex server lives with the plex tests; reuse it without mocks.
PLEX_TESTS_DIR = Path(__file__).resolve().parent.parent / "plex"
if str(PLEX_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(PLEX_TESTS_DIR))
for service in ("sonarr", "radarr"):
    directory = str(Path(__file__).resolve().parent.parent / service)
    if directory not in sys.path:
        sys.path.insert(0, directory)


@pytest.fixture()
def conn(tmp_path):
    from almali_library_curator.db import init_db

    connection = init_db(tmp_path / "lifecycle.db")
    yield connection
    connection.close()
