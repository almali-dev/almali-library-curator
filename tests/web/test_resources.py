"""Request resources are lazy, scoped and reload persisted settings."""
from dataclasses import replace
import sqlite3

import pytest
from flask import g

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings, save_settings


def test_database_is_lazy_reused_and_closed_on_teardown(tmp_path):
    from almali_library_curator.web.resources import get_db
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    app = create_app(settings)
    with app.test_request_context('/'):
        assert 'db' not in g
        conn = get_db()
        assert g.db is conn is get_db()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_settings_reload_for_each_request(tmp_path):
    from almali_library_curator.web.resources import get_settings
    settings = Settings(config_dir=tmp_path, scheduler_enabled=False)
    save_settings(settings)
    app = create_app(settings)
    with app.test_request_context('/'):
        assert get_settings().grace_days == 14
        save_settings(replace(settings, grace_days=30))
        assert get_settings().grace_days == 14
    with app.test_request_context('/'):
        assert get_settings().grace_days == 30
    assert 'scan_scheduler' not in app.extensions
