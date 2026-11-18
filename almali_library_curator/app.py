"""Flask application factory."""

from __future__ import annotations

from datetime import timedelta
import fcntl
from functools import lru_cache
import gzip
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import sqlite3
from secrets import token_bytes
import stat
import tempfile
import time

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import HTTPException, InternalServerError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import safe_join

from almali_library_curator import __version__
from almali_library_curator import auth
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
SECRET_KEY_LOCK_FILE = ".secret_key.lock"
# S17 rule 5: the signing key holds at least 32 bytes; a shorter one is replaced.
MIN_SIGNING_KEY_BYTES = 32


# #110: static assets are versioned with the digest of their own bytes. A URL
# that carries the current digest never changes, so the browser and any proxy
# may keep it for a year; an unversioned or stale URL keeps revalidating with a
# cheap 304 instead of being served a stale asset. The CSS and the JS are
# compressed on demand — gzip is in the standard library, so no new runtime
# dependency — and the compressed bytes are cached until the file changes.
STATIC_VERSION_PARAM = "v"
STATIC_MAX_AGE = 365 * 24 * 60 * 60
# 548 kB of CSS compresses well; below a couple of kilobytes the framing costs
# more than it saves.
MIN_COMPRESS_BYTES = 1024
GZIP_COMPRESS_LEVEL = 6
COMPRESSIBLE_SUFFIXES = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".svg",
        ".json",
        ".map",
        ".txt",
        ".xml",
        ".html",
    }
)


@lru_cache(maxsize=128)
def _asset_bytes(path: str, mtime_ns: int, size: int) -> bytes:
    """One asset's bytes, read again only when the file changes."""
    return Path(path).read_bytes()


@lru_cache(maxsize=128)
def _asset_digest(path: str, mtime_ns: int, size: int) -> str:
    """The content digest a static URL is versioned with."""
    return hashlib.sha256(_asset_bytes(path, mtime_ns, size)).hexdigest()[:20]


@lru_cache(maxsize=64)
def _asset_gzip(path: str, mtime_ns: int, size: int) -> bytes:
    """The compressed form of one asset, kept until the file changes."""
    return gzip.compress(
        _asset_bytes(path, mtime_ns, size), compresslevel=GZIP_COMPRESS_LEVEL, mtime=0
    )


def _asset_file(
    filename: str, static_folder: str | None
) -> tuple[str, os.stat_result] | None:
    """Resolve one static file safely, or ``None`` when it cannot be served."""
    if not static_folder:
        return None
    resolved = safe_join(static_folder, filename)
    if resolved is None:
        return None  # outside the static folder, never read
    try:
        info = os.stat(resolved)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return resolved, info


def asset_version(filename: str, static_folder: str | None) -> str | None:
    """The version token of one static asset, or ``None`` when it is missing."""
    resolved = _asset_file(filename, static_folder)
    if resolved is None:
        return None
    path, info = resolved
    return _asset_digest(path, info.st_mtime_ns, info.st_size)


def _signing_key(config_dir: Path) -> bytes:
    """One persistent, owner-only signing key per installation.

    Every Gunicorn worker and every restart must sign sessions and CSRF
    tokens with the same key, or a token issued by one is refused by another.
    The check and the creation run under an exclusive lock, so concurrent
    workers agree on one key; the key is written whole to a private temporary
    file and then renamed into place, so no reader ever sees a partial key.
    """
    path = Path(config_dir) / SECRET_KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.parent / SECRET_KEY_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) >= MIN_SIGNING_KEY_BYTES:
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                os.chmod(path, 0o600)
            return key
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".secret_key.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token_bytes(MIN_SIGNING_KEY_BYTES))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            os.unlink(temporary)
            raise
        if key:
            logger.warning("signing_key_replaced", extra={"reason": "too_short"})
        return path.read_bytes()
    finally:
        os.close(lock_fd)


def create_app(settings: Settings | None = None, *, scan_runner=None, clock=None,
               wall_clock=None) -> Flask:
    """Build the Flask application.

    ``clock`` (seconds, monotonic) drives the brute-force guard and
    ``wall_clock`` (seconds since the epoch) the absolute session lifetime;
    tests inject them instead of sleeping.
    """
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    app = Flask(__name__)
    trusted_proxies = auth.trusted_proxies_from_environment(os.environ)
    if trusted_proxies:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies)
    app.config["SETTINGS"] = resolved
    app.config["JSON_SORT_KEYS"] = False
    app.config["SECRET_KEY"] = _signing_key(resolved.config_dir)
    # S17 rule 5: the session cookie is never readable by scripts nor sent on
    # cross-site posts; Secure only behind HTTPS (``ALC_SECURE_COOKIES``).
    app.config.update(
        SESSION_COOKIE_NAME="alc_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=auth.secure_cookies_from_environment(os.environ),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=auth.SESSION_HOURS),
        # S21 rule 1: the lifetime is absolute, so the cookie is not slid by
        # the use of the session.
        SESSION_REFRESH_EACH_REQUEST=False,
    )

    @app.after_request
    def _security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Keep normal asset caching and conditional requests; pages, APIs,
        # redirects and errors must not be retained by browsers or proxies.
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    def _static_asset(filename: str):
        """Serve one static asset, versioned and compressed (#110).

        The version in the URL is the asset's own content digest, so a URL
        that still matches is immutable for a year; a missing or stale one
        revalidates. Only the assets that benefit are compressed, and only
        for a client that accepts gzip.
        """
        resolved = _asset_file(filename, app.static_folder)
        if resolved is None:
            abort(404)
        path, info = resolved
        stamp = (path, info.st_mtime_ns, info.st_size)
        digest = _asset_digest(*stamp)
        mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        compressible = (
            info.st_size >= MIN_COMPRESS_BYTES
            and Path(path).suffix.lower() in COMPRESSIBLE_SUFFIXES
        )
        if compressible and request.accept_encodings.quality("gzip") > 0:
            response = app.response_class(_asset_gzip(*stamp), mimetype=mimetype)
            response.set_etag(f"{digest}-gzip")
            response.last_modified = info.st_mtime
            response.headers["Content-Encoding"] = "gzip"
            if request.method in ("GET", "HEAD"):
                response.make_conditional(request.environ)
        else:
            response = send_from_directory(
                app.static_folder, filename, conditional=True, etag=digest
            )
        if compressible:
            # The same URL answers differently per encoding, so caches must key
            # on the request's Accept-Encoding.
            response.vary.add("Accept-Encoding")
        if request.args.get(STATIC_VERSION_PARAM) == digest:
            response.headers["Cache-Control"] = (
                f"public, max-age={STATIC_MAX_AGE}, immutable"
            )
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.view_functions["static"] = _static_asset

    @app.template_global()
    def asset_url(filename: str) -> str:
        """A static URL carrying the asset's current content version (#110)."""
        version = asset_version(filename, app.static_folder)
        return url_for(
            "static",
            filename=filename,
            **({STATIC_VERSION_PARAM: version} if version else {}),
        )

    # S17 rule 1: read once at start; only the hash is kept, in memory.
    credentials = auth.credentials_from_environment(os.environ, app.config["SECRET_KEY"])
    guard = auth.LoginGuard(clock)
    # S21 rules 1 and 2: the wall clock of the sign-in and the revocation
    # epoch, shared by every worker through the config directory.
    session_clock = wall_clock or time.time
    epoch = auth.SessionEpoch(resolved.config_dir / auth.SESSION_EPOCH_FILE)
    forwarded_warned = False

    @app.before_request
    def _warn_forwarded_without_trusted_proxies():
        # #71: X-Forwarded-For with ALC_TRUSTED_PROXIES unset tells the owner
        # that every client shares the proxy's address, so the login lockout is
        # global. Reported once per process; the header never changes the
        # client address by itself.
        nonlocal forwarded_warned
        if trusted_proxies or forwarded_warned:
            return None
        if auth.forwarded_headers_present(request.headers):
            forwarded_warned = True
            logger.warning(
                "forwarded_headers_without_trusted_proxies",
                extra={"header": auth.FORWARDED_HEADER, "client": request.remote_addr or "unknown"},
            )
        return None

    @app.before_request
    def _login_configured():
        # S17 rule 2: without a login nothing is served open. Registered before
        # the CSRF check, so every action answers the same 503 page.
        if credentials is None and request.endpoint not in auth.ALWAYS_PUBLIC_ENDPOINTS:
            return _error_page("auth_missing", 503)
        return None

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

    def _signed_in() -> bool:
        if credentials is None:
            return False
        # S21 rule 1: the session ends SESSION_HOURS after the sign-in, however
        # much it is used; a session without the sign-in time is expired.
        if auth.session_expired(session.get(auth.SESSION_SIGNED_IN_AT), session_clock()):
            return False
        # S21 rule 2: the epoch in force revokes every session issued before
        # the last sign-out, in every browser.
        return credentials.signed_in(
            session.get(auth.SESSION_USER),
            session.get(auth.SESSION_PROOF),
            epoch.current(),
        )

    @app.before_request
    def _require_login():
        # S17 rule 3: only /healthz, /static/* and /login are public.
        if request.endpoint in auth.PUBLIC_ENDPOINTS or _signed_in():
            return None
        if request.endpoint == "scan":
            return jsonify({"error": "login_required"}), 401
        if request.method in ("GET", "HEAD"):
            target = request.full_path if request.query_string else request.path
            return redirect(url_for("login", next=target), 303)
        return redirect(url_for("login"), 303)

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

        # S1e (#47): the settings screen offers exactly these languages.
        return {"locale": locale, "locales": SUPPORTED_LOCALES, "t": t}

    @app.get("/healthz")
    def healthz():
        ready = _database_ready(resolved.database_path)
        # S15 rule 8: without Plex the app is alive but not ready. The status
        # code and ``status`` stay the liveness answer, so a fresh install is
        # never reported unhealthy before the owner has connected Plex.
        current = get_settings()
        missing = [] if current.plex_base_url and current.plex_token else ["plex"]
        if credentials is None:
            missing.append("auth")
        payload = {
            "status": "ok" if ready else "degraded",
            "ready": ready and not missing,
            "missing": missing,
        }
        return jsonify(payload), 200 if ready else 503

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = auth.safe_next(request.values.get("next"))
        if request.method == "GET":
            return render_template("login.html", next_path=target)
        address = request.remote_addr or "unknown"
        attempt = guard.admit(address)
        if attempt is None:
            logger.warning("auth_login_locked", extra={"client": address})
            return render_template("login.html", next_path=target, message="login.locked"), 429
        if credentials.matches(request.form.get("user", ""), request.form.get("password", "")):
            guard.succeeded(address, attempt)
            session.clear()  # no session fixation
            session[auth.SESSION_USER] = credentials.user
            session[auth.SESSION_PROOF] = credentials.proof(epoch.ensure())
            # S21 rule 1: the sign-in time travels with the session, so every
            # worker and every restart read the same absolute lifetime.
            session[auth.SESSION_SIGNED_IN_AT] = session_clock()
            session.permanent = True
            logger.info("auth_login_succeeded", extra={"client": address, "user": credentials.safe_log_user()})
            return redirect(target, 303)
        guard.failed(address, attempt)
        logger.warning("auth_login_failed", extra={"client": address})
        return render_template("login.html", next_path=target, message="login.failed"), 401

    @app.post("/logout")
    def logout():
        # S21 rule 2: the sign-out ends every session, in every browser; the
        # epoch advances and every session issued so far stops matching.
        revoked = epoch.advance()
        session.clear()
        client = request.remote_addr or "unknown"
        if not revoked:
            logger.warning("auth_logout_revoke_failed", extra={"client": client})
            return _error_page("logout_revoke_failed", 503)
        logger.info("auth_logout", extra={"client": client})
        return redirect(url_for("login"), 303)

    @app.post("/scan")
    def scan():
        accepted = runner.start_background()
        return jsonify({"status": "accepted" if accepted else "running"}), 202 if accepted else 409

    from almali_library_curator.web.routes import bp
    app.register_blueprint(bp)

    return app
