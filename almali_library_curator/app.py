"""Flask application factory."""

from __future__ import annotations

import logging
import sqlite3
from secrets import token_bytes

from flask import Flask, g, jsonify, request

from almali_library_curator import __version__
from almali_library_curator.config import Settings, load_settings
from almali_library_curator.db import SCHEMA_VERSION, connect, init_db
from almali_library_curator.i18n import SUPPORTED_LOCALES, load_catalogs, translate
from almali_library_curator.logging_config import configure_logging
from almali_library_curator.scan import ScanRunner
from almali_library_curator.scan.scheduler import ScanScheduler

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


def create_app(settings: Settings | None = None, *, scan_runner=None) -> Flask:
    """Build the Flask application."""
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    app = Flask(__name__)
    app.config["SETTINGS"] = resolved
    app.config["JSON_SORT_KEYS"] = False
    # Ephemeral session signing until T11 supplies install-time credentials.
    app.config["SECRET_KEY"] = token_bytes(32)
    from almali_library_curator.web.resources import close_db, get_settings
    app.teardown_appcontext(close_db)

    catalogs = load_catalogs()

    init_db(resolved.database_path).close()
    runner = scan_runner if scan_runner is not None else ScanRunner(resolved)
    app.extensions["scan_runner"] = runner
    if resolved.scheduler_enabled:
        scheduler = ScanScheduler(runner)
        app.extensions["scan_scheduler"] = scheduler
        scheduler.start()
    logger.info(
        "application_started",
        extra={"version": __version__, "schema_version": SCHEMA_VERSION, "dry_run": resolved.dry_run},
    )

    @app.before_request
    def _select_locale() -> None:
        requested = request.args.get("lang") or request.accept_languages.best_match(
            SUPPORTED_LOCALES
        )
        g.locale = requested if requested in SUPPORTED_LOCALES else get_settings().locale

    @app.context_processor
    def _inject_translations() -> dict:
        locale = g.get("locale", resolved.locale)

        def t(key: str, **params) -> str:
            value = translate(key, locale, catalogs)
            return value.format(**params) if params else value

        return {"locale": locale, "t": t}

    @app.get("/healthz")
    def healthz():
        ready = _database_ready(resolved.database_path)
        payload = {
            "status": "ok" if ready else "degraded",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
        }
        return jsonify(payload), 200 if ready else 503

    @app.post("/scan")
    def scan():
        accepted = runner.start_background()
        return jsonify({"status": "accepted" if accepted else "running"}), 202 if accepted else 409

    from almali_library_curator.web.routes import bp
    app.register_blueprint(bp)

    return app
