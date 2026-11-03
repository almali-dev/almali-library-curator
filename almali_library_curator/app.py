"""Flask application factory."""

from __future__ import annotations

import logging
import sqlite3

from flask import Flask, g, jsonify, render_template, request

from almali_library_curator import __version__
from almali_library_curator.config import Settings, load_settings
from almali_library_curator.db import SCHEMA_VERSION, connect, init_db
from almali_library_curator.i18n import SUPPORTED_LOCALES, load_catalogs, translate
from almali_library_curator.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _database_ready(database_path) -> bool:
    """True when the database exists and carries the expected schema."""
    try:
        conn = connect(database_path)
    except sqlite3.Error:
        logger.warning("database_unavailable", extra={"database_path": str(database_path)})
        return False
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
            )
        }
        return version == SCHEMA_VERSION and bool(tables)
    except sqlite3.Error:
        logger.warning("database_unreadable", extra={"database_path": str(database_path)})
        return False
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> Flask:
    """Build the Flask application."""
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    app = Flask(__name__)
    app.config["SETTINGS"] = resolved
    app.config["JSON_SORT_KEYS"] = False

    catalogs = load_catalogs()

    init_db(resolved.database_path).close()
    logger.info(
        "application_started",
        extra={"version": __version__, "schema_version": SCHEMA_VERSION, "dry_run": resolved.dry_run},
    )

    @app.before_request
    def _select_locale() -> None:
        requested = request.args.get("lang") or request.accept_languages.best_match(
            SUPPORTED_LOCALES
        )
        g.locale = requested if requested in SUPPORTED_LOCALES else resolved.locale

    @app.context_processor
    def _inject_translations() -> dict:
        locale = g.get("locale", resolved.locale)
        return {
            "locale": locale,
            "t": lambda key: translate(key, locale, catalogs),
        }

    @app.get("/healthz")
    def healthz():
        ready = _database_ready(resolved.database_path)
        payload = {
            "status": "ok" if ready else "degraded",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
        }
        return jsonify(payload), 200 if ready else 503

    @app.get("/")
    def index():
        return render_template("index.html")

    return app
