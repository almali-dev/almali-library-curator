"""Flask application factory."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
from secrets import token_bytes
import tempfile

from flask import Flask, g, jsonify, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import HTTPException, InternalServerError

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


SECRET_KEY_FILE = "secret_key"


def _signing_key(config_dir: Path) -> bytes:
    """One persistent, owner-only signing key per installation.

    Every Gunicorn worker and every restart must sign sessions and CSRF
    tokens with the same key, or a token issued by one is refused by another.
    """
    path = Path(config_dir) / SECRET_KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    # Write the whole key to a private temporary file, then publish it with
    # link(), which fails if another worker published first: no reader can
    # ever see a created-but-unwritten key file.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".secret_key.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(token_bytes(32))
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    return path.read_bytes()


def create_app(settings: Settings | None = None, *, scan_runner=None) -> Flask:
    """Build the Flask application."""
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    app = Flask(__name__)
    app.config["SETTINGS"] = resolved
    app.config["JSON_SORT_KEYS"] = False
    app.config["SECRET_KEY"] = _signing_key(resolved.config_dir)
    # Every mutating route (form or JSON) needs the token the screens embed.
    CSRFProtect(app)
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

    def _requested_locale() -> str | None:
        requested = request.args.get("lang") or request.accept_languages.best_match(
            SUPPORTED_LOCALES
        )
        return requested if requested in SUPPORTED_LOCALES else None

    @app.before_request
    def _select_locale() -> None:
        g.locale = _requested_locale() or get_settings().locale

    def _error_page(kind: str, status: int, error: HTTPException | None = None):
        # The CSRF check runs before the locale is chosen, so choose it here,
        # without loading settings: a refused request must change nothing.
        if "locale" not in g:
            g.locale = _requested_locale() or resolved.locale
        g.error_page = True
        response = app.make_response((render_template("error.html", kind=kind), status))
        allowed = getattr(error, "valid_methods", None)
        if allowed:
            response.headers["Allow"] = ", ".join(allowed)
        return response

    @app.errorhandler(404)
    def _not_found(error):
        return _error_page("not_found", 404, error)

    @app.errorhandler(405)
    def _method_not_allowed(error):
        return _error_page("method_not_allowed", 405, error)

    @app.errorhandler(CSRFError)
    def _csrf_refused(error):
        # Same status as Flask-WTF's own answer (400); nothing has run.
        return _error_page("csrf", 400, error)

    @app.errorhandler(InternalServerError)
    def _server_error(error):
        # Never the exception text: it may carry details the owner must not see.
        return _error_page("server", 500, error)

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
        # S15 rule 8: without Plex the app is alive but not ready. The status
        # code and ``status`` stay the liveness answer, so a fresh install is
        # never reported unhealthy before the owner has connected Plex.
        current = get_settings()
        missing = [] if current.plex_base_url and current.plex_token else ["plex"]
        payload = {
            "status": "ok" if ready else "degraded",
            "ready": ready and not missing,
            "missing": missing,
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
