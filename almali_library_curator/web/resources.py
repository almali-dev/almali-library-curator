"""Lazy request-local resources; no connection or settings cross a request."""
from dataclasses import replace

from flask import current_app, g

from almali_library_curator.scan.settings import load_runtime_settings
from almali_library_curator.db import connect


def get_settings():
    if 'settings' not in g:
        initial = current_app.config['SETTINGS']
        g.settings = replace(load_runtime_settings(initial.config_dir), database_path=initial.database_path)
    return g.settings


def get_db():
    if 'db' not in g:
        g.db = connect(get_settings().database_path)
    return g.db


def close_db(error=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()
