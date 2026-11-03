"""Reuse the existing HTTP fakes at the service boundary."""
import sys
from pathlib import Path

import pytest

from almali_library_curator.db import init_db

for service in ("plex", "radarr", "sonarr", "tautulli", "tmdb"):
    directory = str(Path(__file__).resolve().parent.parent / service)
    if directory not in sys.path:
        sys.path.insert(0, directory)


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "enrichment.db")
    yield connection
    connection.close()
