from __future__ import annotations

import pytest
from dataclasses import replace

from almali_library_curator.app import create_app


@pytest.fixture()
def app(settings):
    application = create_app(replace(settings, scheduler_enabled=False))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()
