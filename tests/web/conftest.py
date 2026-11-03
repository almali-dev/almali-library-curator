"""Web fixtures keep the scheduler off and use a real migrated database."""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from almali_library_curator.app import create_app
from almali_library_curator.config import save_settings
from almali_library_curator.db import connect

for name in ('plex', 'lifecycle', 'radarr', 'sonarr', 'tautulli', 'tmdb'):
    directory = str(Path(__file__).resolve().parent.parent / name)
    if directory not in sys.path:
        sys.path.insert(0, directory)


@pytest.fixture
def app(settings):
    settings = replace(settings, scheduler_enabled=False)
    save_settings(settings)
    app = create_app(settings)
    app.config.update(TESTING=True, SECRET_KEY='web-test-session')
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def conn(app):
    db = connect(app.config['SETTINGS'].database_path)
    yield db
    db.close()
