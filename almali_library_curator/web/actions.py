"""POST-only owner actions. The lifecycle/protection engines own all SQL writes."""
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

from flask import abort, current_app, flash, g, redirect, render_template, request, url_for

from almali_library_curator import library_scope, secret_store
from almali_library_curator.config import (
    PLEX_TOKEN_STORED, SERVICE_KEY_FIELDS, save_settings, update_settings_file,
)
from almali_library_curator.detection.models import Candidate, Unit
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.models import (
    DuplicateMarkError, GraceRangeError, ProtectedItemError, RequestStateError,
    UncuratedItemError,
)
from almali_library_curator.lifecycle.safety import set_kill_switch
from almali_library_curator.plex import PlexClient, auth
from almali_library_curator.plex.client import PlexAuthError, PlexError, PlexUnavailableError
from almali_library_curator.protection import ProtectionEngine
from almali_library_curator.radarr import RadarrAuthError, RadarrClient, RadarrError, RadarrUnavailableError
from almali_library_curator.scan.pipeline import build_detection
from almali_library_curator.sonarr import SonarrAuthError, SonarrClient, SonarrError, SonarrUnavailableError
from almali_library_curator.tautulli import TautulliAuthError, TautulliClient, TautulliError, TautulliUnavailableError
from almali_library_curator.tmdb import TmdbAuthError, TmdbClient, TmdbError, TmdbUnavailableError
from almali_library_curator.tmdb.client import DEFAULT_BASE_URL as TMDB_DEFAULT_BASE_URL
from almali_library_curator.web.forms import (
    boolean, validate_plex_connection, validate_service_connection, validate_settings,
)
from almali_library_curator.web.resources import get_db, get_settings
from almali_library_curator.web.views import (
    SERVICE_BRANDS, connections_view, now, plex_form_view, plex_signin_view, service_forms_view,
    settings_view,
)

logger = logging.getLogger(__name__)


class InvalidScope(ValueError):
    """The submitted scope does not describe a synced media row."""


class NotCandidate(ValueError):
    """A fresh detection pass no longer matches the submitted scope."""


def _scope(identifier):
    conn = get_db()
    row = conn.execute('SELECT * FROM media_items WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise InvalidScope()
    try:
        unit = Unit(request.form.get('unit', row['item_type']))
        numbers = tuple(sorted({int(part) for raw in request.form.getlist('seasons')
                                for part in raw.split(',') if part.strip()}))
    except ValueError as error:
        raise InvalidScope() from error
    if unit == Unit.SEASONS:
        known = {r[0] for r in conn.execute('SELECT DISTINCT season_number FROM episodes'
                                           ' WHERE series_rating_key=?', (row['plex_rating_key'],))}
        if row['item_type'] != 'series' or not numbers or not set(numbers) <= known:
            raise InvalidScope()
    elif numbers or unit.value != row['item_type']:
        raise InvalidScope()
    return Candidate(identifier, row['plex_rating_key'], row['title'], unit, numbers, (),
                     row['file_size_bytes'] or 0, row['rating'], {})


def _fresh_candidate(scope):
    detection = build_detection(get_db(), get_settings(), now)
    outcome = detection.scan_with_protection()
    rows = [*outcome.candidates, *(row.candidate for row in outcome.protected)]
    for row in rows:
        if (row.media_item_id, row.unit, row.seasons) == (scope.media_item_id, scope.unit, scope.seasons):
            return row
    raise NotCandidate()


def _engine():
    settings = get_settings()
    plex = PlexClient.from_settings(settings) if settings.plex_base_url and settings.plex_token else None
    return LifecycleEngine(get_db(), settings=settings, clock=now, collections=plex,
                           protection=ProtectionEngine(get_db(), settings=settings))


def _result(key, endpoint='web.library', *, flash_params=None, **params):
    # Keys stay in the session; the shell translates at rendering time.
    # A message with a placeholder is translated here, in the reader's
    # language, because the shell translates a flash key without parameters.
    message = key
    if flash_params:
        message = translate(key, g.get('locale', get_settings().locale)).format(**flash_params)
    flash(message)
    return redirect(url_for(endpoint, lang=g.get('locale', get_settings().locale), **params), code=303)


def _perform(action, success, endpoint='web.library', *, flash_params=None, **params):
    try:
        action()
        key = success
    except DuplicateMarkError:
        key = 'web.flash.duplicate_mark'
    except ProtectedItemError:
        key = 'web.flash.protected'
    except GraceRangeError:
        key = 'web.flash.grace_range'
    except RequestStateError:
        key = 'web.flash.request_state'
    except InvalidScope:
        key = 'web.flash.invalid_scope'
    except NotCandidate:
        key = 'web.flash.not_candidate'
    except UncuratedItemError:
        key = 'web.flash.not_curated'
    except Exception as error:
        # A collection write may fail AFTER the durable engine transition.
        # Do not claim success, roll it back, or expose an external error body.
        get_db().rollback()
        logger.warning('web_action_failed', extra={'error_type': type(error).__name__})
        key = 'web.flash.action_failed'
    return _result(key, endpoint, flash_params=flash_params, **params)


# One attempt, bounded: the owner is waiting on the form (and on the worker).
PLEX_TEST_TIMEOUT_SECONDS = 10.0
# Where each Plex form posts back to: the wizard's Plex step or Connections.
PLEX_ORIGINS = {'setup': ('web.setup', {'step': 'plex'}), 'connections': ('web.connections', {})}


def _plex_origin():
    return 'setup' if request.form.get('origin') == 'setup' else 'connections'


def _redirect_origin(origin):
    endpoint, params = PLEX_ORIGINS[origin]
    return redirect(url_for(endpoint, lang=g.get('locale', get_settings().locale), **params), code=303)


def _plex_page(origin, status, *, errors=None, base_url=None, servers=()):
    """Re-render the page the Plex form came from; nothing has been stored."""
    from almali_library_curator.web.routes import WIZARD_STEPS
    conn, current = get_db(), get_settings()
    signin = plex_signin_view(read_signin_state(current.config_dir), servers=servers)
    plex = plex_form_view(current, errors=errors, base_url=base_url, signin=signin)
    services = connections_view(conn, current, probe=False)
    forms = service_forms_view(current)
    if origin == 'setup':
        return render_template('web/setup.html', step='plex', steps=WIZARD_STEPS, connections=services,
                               model=settings_view(conn, current), tested=False, plex=plex,
                               service_forms=forms), status
    return render_template('web/connections.html', model=services, tested=False, plex=plex,
                           service_forms=forms), status


def _test_plex(settings, address, token):
    """None when Plex answers with a server identity, else the error key (S12 rule 3)."""
    client = PlexClient(address, token,
                        timeout=min(settings.plex_timeout_seconds, PLEX_TEST_TIMEOUT_SECONDS),
                        rate_limit_per_second=0, max_retries=0, backoff_seconds=0)
    try:
        client.server_identity()
        return None
    except PlexAuthError:
        outcome = 'token_refused'
    except PlexUnavailableError:
        outcome = 'unreachable'
    except PlexError:
        outcome = 'not_a_server'
    except Exception as error:
        logger.warning('plex_connection_test_error', extra={'error_type': type(error).__name__})
        outcome = 'not_a_server'
    logger.info('plex_connection_test_failed', extra={'outcome': outcome})
    return 'web.form.plex_' + outcome


# -- the optional service keys (S20) ------------------------------------------
#
# One key per optional service, entered in the app, tested against the
# service and stored encrypted in the same Fernet store as the Plex token.

# One attempt, bounded: the owner is waiting on the form (S20 rule 3).
SERVICE_TEST_TIMEOUT_SECONDS = 10.0
# Where each service form posts back to: the wizard's connections step or
# Connections.
SERVICE_ORIGINS = {'setup': ('web.setup', {'step': 'connections'}),
                   'connections': ('web.connections', {})}
# The client of every service, with the errors it raises: the key was
# refused, the service could not be reached, or it answered something else.
SERVICE_CLIENTS = {
    'sonarr': (SonarrClient, SonarrAuthError, SonarrUnavailableError, SonarrError),
    'radarr': (RadarrClient, RadarrAuthError, RadarrUnavailableError, RadarrError),
    'tautulli': (TautulliClient, TautulliAuthError, TautulliUnavailableError, TautulliError),
    'tmdb': (TmdbClient, TmdbAuthError, TmdbUnavailableError, TmdbError),
}


def _service_origin():
    return 'setup' if request.form.get('origin') == 'setup' else 'connections'


def _services_page(origin, status, *, errors=None, service=None, base_url=None):
    """Re-render the page the service form came from; nothing has been stored."""
    from almali_library_curator.web.routes import WIZARD_STEPS
    conn, current = get_db(), get_settings()
    forms = service_forms_view(current, errors=errors, service=service, base_url=base_url)
    if origin == 'setup':
        return render_template('web/setup.html', step='connections', steps=WIZARD_STEPS,
                               connections=connections_view(conn, current, probe=False),
                               model=settings_view(conn, current), tested=False,
                               plex=plex_form_view(current), service_forms=forms), status
    return render_template('web/connections.html',
                           model=connections_view(conn, current, probe=False), tested=False,
                           plex=plex_form_view(current), service_forms=forms), status


def _test_service(service, address, key):
    """None when the service answers with its own API, else the error key.

    One attempt, bounded: the owner is waiting on the form (S20 rule 3).
    """
    client_class, auth_error, unavailable_error, other_error = SERVICE_CLIENTS[service]
    client = client_class(address, key, timeout=SERVICE_TEST_TIMEOUT_SECONDS,
                          rate_limit_per_second=0, max_retries=0, backoff_seconds=0)
    try:
        if service == 'tautulli':
            client.users()
        elif service == 'tmdb':
            client.movie(550)
        else:
            client.system_status()
        return None
    except auth_error:
        outcome = 'key_refused'
    except unavailable_error:
        outcome = 'unreachable'
    except other_error:
        outcome = 'not_service'
    except Exception as error:
        logger.warning('service_connection_test_error',
                       extra={'service': service, 'error_type': type(error).__name__})
        outcome = 'not_service'
    logger.info('service_connection_test_failed', extra={'service': service, 'outcome': outcome})
    return 'web.form.service_' + outcome


# -- the Plex sign-in state (S13) ----------------------------------------------
#
# The pending sign-in lives in ``<config_dir>/plex-signin.json`` (owner-only,
# atomically replaced) so every Gunicorn worker can render it. It holds the
# PIN id, the code and translation keys only: the code is not a credential
# and the auth token never reaches this file, a page or a log line.

SIGNIN_FILE_NAME = 'plex-signin.json'
# Poll every two seconds for at most fifteen minutes: the PIN's own
# lifetime (S13 rule 1).
SIGNIN_POLL_INTERVAL_SECONDS = 2.0
SIGNIN_POLL_ATTEMPTS = 450


def read_signin_state(config_dir):
    """The stored sign-in state, or None. Never raises and never holds a token."""
    try:
        payload = json.loads((Path(config_dir) / SIGNIN_FILE_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_signin_state(config_dir) -> None:
    (Path(config_dir) / SIGNIN_FILE_NAME).unlink(missing_ok=True)


def take_signin_state(config_dir):
    """Read the state; a terminal one is cleared so its message shows once."""
    state = read_signin_state(config_dir)
    if state and state.get('state') in ('succeeded', 'failed', 'cancelled'):
        clear_signin_state(config_dir)
    return state


def _write_signin_state(config_dir, state) -> None:
    path = Path(config_dir) / SIGNIN_FILE_NAME
    data = (json.dumps(state, indent=2, sort_keys=True) + '\n').encode('utf-8')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{SIGNIN_FILE_NAME}.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _finish_signin(config_dir, state, message=None) -> None:
    current = read_signin_state(config_dir) or {}
    merged = {key: current.get(key) for key in ('version', 'pin_id', 'code', 'client_identifier')}
    merged['state'] = state
    merged['message'] = message
    _write_signin_state(config_dir, merged)


def _poll_signin(config_dir, settings, plex_tv_base_url, interval, attempts):
    """Poll plex.tv until the token arrives, then store exactly like T9a.

    Runs in the background thread of the request that started the sign-in,
    outside any request context: everything it needs was captured then. A
    cancel wins over an arrived token -- the state is re-read before
    anything is stored -- and every failure is explicit and stores nothing
    (S13 rules 1, 4 and 5).
    """
    try:
        token = None
        for _ in range(attempts):
            state = read_signin_state(config_dir)
            if state is None or state.get('state') != 'pending':
                return  # cancelled or replaced: store nothing
            try:
                token = auth.poll_pin(state['pin_id'], base_url=plex_tv_base_url)
            except auth.PlexPinExpiredError:
                _finish_signin(config_dir, 'failed', 'web.form.plex_signin_expired')
                return
            except auth.PlexTvRefusedError:
                _finish_signin(config_dir, 'failed', 'web.form.plex_signin_refused')
                return
            except auth.PlexTvError:
                _finish_signin(config_dir, 'failed', 'web.form.plex_signin_unreachable')
                return
            if token:
                break
            time.sleep(interval)
        else:
            # Fifteen minutes of polling is the PIN's own lifetime.
            _finish_signin(config_dir, 'failed', 'web.form.plex_signin_expired')
            return
        state = read_signin_state(config_dir)
        if state is None or state.get('state') != 'pending':
            return  # cancelled while the last poll was in flight
        address = settings.plex_base_url
        if address:
            # Test the arrived token against the configured server with the
            # existing client, then store exactly like the manual flow.
            failure = _test_plex(settings, address, token)
            if failure:
                _finish_signin(config_dir, 'failed', failure)
                return
            secret_store.store_plex_token(config_dir, token)
            update_settings_file(config_dir, {'plex_base_url': address})
            _finish_signin(config_dir, 'succeeded', 'connections.plex.signin.succeeded')
            logger.info('plex_signin_completed', extra={'address_saved': True})
        else:
            # No address yet: the token is stored encrypted now, and the
            # owner picks the address among their own servers (S13 rule 3).
            # The app never writes an address the owner did not pick.
            secret_store.store_plex_token(config_dir, token)
            _finish_signin(config_dir, 'authorized')
            logger.info('plex_signin_completed', extra={'address_saved': False})
    except Exception as error:
        logger.warning('plex_signin_failed', extra={'error_type': type(error).__name__})
        _finish_signin(config_dir, 'failed', 'web.form.plex_signin_unreachable')


def register_plex_actions(bp):
    @bp.post('/connections/plex')
    def save_plex():
        origin, current = _plex_origin(), get_settings()
        if current.plex_env_managed:
            # The page shows the environment note in place of the form.
            return _plex_page(origin, 409)
        stored = current.plex_token if current.plex_token_status == PLEX_TOKEN_STORED else ''
        address, token, changed, errors = validate_plex_connection(request.form, stored_token=stored)
        if errors:
            submitted = (request.form.get('plex_base_url') or '').strip()
            # Never echo user info (a credential) typed into the address.
            return _plex_page(origin, 400, errors=errors, base_url='' if '@' in submitted else submitted)
        failure = _test_plex(current, address, token)
        if failure:
            return _plex_page(origin, 400, errors={'plex': failure}, base_url=address)

        def action():
            if changed:
                secret_store.store_plex_token(current.config_dir, token)
            update_settings_file(current.config_dir, {'plex_base_url': address})
            # A completed manual save (or a picked address) ends any pending
            # sign-in: the manual form stays usable at all times (S13 rule 5).
            clear_signin_state(current.config_dir)
            logger.info('plex_connection_saved', extra={'plex_credential_replaced': changed})

        endpoint, params = PLEX_ORIGINS[origin]
        return _perform(action, 'web.flash.plex_saved', endpoint, **params)

    @bp.post('/connections/plex/signin')
    def start_plex_signin():
        """Start the PIN flow: create the PIN and poll in the background."""
        origin, current = _plex_origin(), get_settings()
        if current.plex_env_managed:
            return _plex_page(origin, 409)
        state = read_signin_state(current.config_dir)
        if state and state.get('state') in ('pending', 'authorized'):
            endpoint, params = PLEX_ORIGINS[origin]
            return _result('web.flash.plex_signin_pending', endpoint, **params)
        try:
            pin = auth.create_pin(base_url=auth.PLEX_TV_BASE_URL)
        except auth.PlexTvRefusedError:
            return _plex_page(origin, 400, errors={'signin': 'web.form.plex_signin_refused'})
        except auth.PlexTvError:
            return _plex_page(origin, 400, errors={'signin': 'web.form.plex_signin_unreachable'})
        _write_signin_state(current.config_dir, {
            'version': 1,
            'pin_id': pin.id,
            'code': pin.code,
            'client_identifier': auth.CLIENT_IDENTIFIER,
            'state': 'pending',
            'message': None,
        })
        threading.Thread(
            target=_poll_signin,
            args=(current.config_dir, current, auth.PLEX_TV_BASE_URL,
                  SIGNIN_POLL_INTERVAL_SECONDS, SIGNIN_POLL_ATTEMPTS),
            daemon=True,
            name='plex-signin-poll',
        ).start()
        logger.info('plex_signin_started')
        return _redirect_origin(origin)

    @bp.post('/connections/plex/signin/cancel')
    def cancel_plex_signin():
        """Cancel the pending sign-in. The poll thread stores nothing (S13
        rule 4), and a token already stored on arrival is forgotten, so the
        whole sign-in is undone."""
        origin, current = _plex_origin(), get_settings()
        state = read_signin_state(current.config_dir)
        if state and state.get('state') in ('pending', 'authorized'):
            if state.get('state') == 'authorized':
                secret_store.forget_plex_token(current.config_dir)
            _finish_signin(current.config_dir, 'cancelled')
            logger.info('plex_signin_cancelled')
        return _redirect_origin(origin)

    @bp.post('/connections/plex/signin/servers')
    def plex_signin_servers():
        """Offer the owner's own servers from plex.tv (S13 rule 3)."""
        origin, current = _plex_origin(), get_settings()
        if current.plex_env_managed:
            return _plex_page(origin, 409)
        state = read_signin_state(current.config_dir)
        if not state or state.get('state') != 'authorized':
            return _redirect_origin(origin)
        if current.plex_token_status != PLEX_TOKEN_STORED or not current.plex_token:
            # The stored token was forgotten meanwhile; start again.
            return _plex_page(origin, 400, errors={'signin': 'web.form.plex_signin_expired'})
        try:
            servers = auth.list_resources(current.plex_token, base_url=auth.PLEX_TV_BASE_URL)
        except auth.PlexTvError:
            logger.info('plex_resources_failed')
            return _plex_page(origin, 400, errors={'signin': 'web.form.plex_resources_unreachable'})
        return _plex_page(origin, 200, servers=servers)

    @bp.post('/connections/plex/forget')
    def forget_plex():
        origin, current = _plex_origin(), get_settings()
        if current.plex_env_managed:
            return _plex_page(origin, 409)

        def action():
            secret_store.forget_plex_token(current.config_dir)
            # Without a token an authorized sign-in cannot continue either.
            clear_signin_state(current.config_dir)
            logger.info('plex_token_forgotten')

        endpoint, params = PLEX_ORIGINS[origin]
        return _perform(action, 'web.flash.plex_forgotten', endpoint, **params)


def register_service_actions(bp):
    """The optional service keys, entered in the app (S20).

    One key per service, tested against the service before it is stored and
    kept encrypted in the same Fernet store as the Plex token. The key never
    reaches a page, a flash message or a log line (S20 rules 3 to 7).
    """

    @bp.post('/connections/<service>')
    def save_service(service):
        if service not in SERVICE_KEY_FIELDS:
            abort(404)
        origin, current = _service_origin(), get_settings()
        if service in current.env_managed_services:
            # The page shows the environment note in place of the form.
            return _services_page(origin, 409, service=service)
        key_field = SERVICE_KEY_FIELDS[service]
        # Only a key that came from the encrypted store can be kept; an
        # unreadable entry reads as no key at all (S20 rule 9).
        stored = getattr(current, key_field) if key_field in current.stored_secrets else ''
        address, key, changed, errors = validate_service_connection(
            request.form, service, stored_key=stored)
        if errors:
            submitted = (request.form.get(service + '_base_url') or '').strip()
            # Never echo user info, query/fragment secrets or any rejected address.
            unsafe = (service + '_base_url') in errors or any(c in submitted for c in '@?#')
            return _services_page(origin, 400, errors=errors, service=service,
                                  base_url='' if unsafe else submitted)
        if service == 'tmdb':
            # TMDB asks for no address: its own default stays (S20 rule 1).
            address = current.tmdb_base_url or TMDB_DEFAULT_BASE_URL
        failure = _test_service(service, address, key)
        if failure:
            return _services_page(origin, 400, errors={'service': failure}, service=service,
                                  base_url=address)

        def action():
            with secret_store.writer_lock(current.config_dir):
                # Pair the key and address atomically so concurrent saves never
                # mix them and a failed settings write rolls the store back.
                state = secret_store.read_secret(current.config_dir, key_field)
                if not changed and (state.status != secret_store.STORED or state.token != key):
                    raise RuntimeError('stored service key changed during the connection test')
                need_store = changed
                prior = secret_store.snapshot_store(current.config_dir) if need_store else None
                if need_store:
                    secret_store.store_secret(current.config_dir, key_field, key)
                try:
                    if service != 'tmdb':
                        update_settings_file(current.config_dir, {service + '_base_url': address})
                except BaseException:
                    if need_store:
                        secret_store.restore_store(current.config_dir, prior)
                    raise
            logger.info('service_connection_saved',
                        extra={'service': service, 'service_key_replaced': changed})

        endpoint, params = SERVICE_ORIGINS[origin]
        return _perform(action, 'web.flash.service_saved', endpoint,
                        flash_params={'service': SERVICE_BRANDS[service]}, **params)

    @bp.post('/connections/<service>/forget')
    def forget_service(service):
        if service not in SERVICE_KEY_FIELDS:
            abort(404)
        origin, current = _service_origin(), get_settings()
        if service in current.env_managed_services:
            return _services_page(origin, 409, service=service)

        def action():
            secret_store.forget_secret(current.config_dir, SERVICE_KEY_FIELDS[service])
            logger.info('service_key_forgotten', extra={'service': service})

        endpoint, params = SERVICE_ORIGINS[origin]
        return _perform(action, 'web.flash.service_forgotten', endpoint,
                        flash_params={'service': SERVICE_BRANDS[service]}, **params)


def register_actions(bp):
    register_plex_actions(bp)
    register_service_actions(bp)

    @bp.post('/items/<int:media_item_id>/mark')
    def mark(media_item_id):
        def action():
            scope = _scope(media_item_id)
            # S14 rule 6: nothing outside the curated scope may be marked.
            # Checked before the fresh detection pass so the owner sees the
            # scope refusal, not the generic no-longer-a-candidate one.
            if not library_scope.item_is_curated(get_db(), get_settings(), media_item_id):
                raise UncuratedItemError('outside the curated scope')
            candidate = _fresh_candidate(scope)
            days = request.form.get('grace_days')
            try:
                days = int(days) if days not in (None, '') else None
            except ValueError as error:
                raise GraceRangeError() from error
            _engine().mark_for_removal(candidate, grace_days=days, marked_by='owner')
        return _perform(action, 'web.flash.marked')

    def cancel(request_id, keep):
        def action():
            engine = _engine()
            try:
                removal = engine._request_by_id(request_id)
            except Exception as error:
                raise RequestStateError() from error
            (engine.keep_it if keep else engine.undo)(removal)
        return _perform(action, 'web.flash.kept' if keep else 'web.flash.undone')

    @bp.post('/requests/<int:request_id>/undo')
    def undo(request_id):
        return cancel(request_id, False)

    @bp.post('/requests/<int:request_id>/keep')
    def keep(request_id):
        return cancel(request_id, True)

    @bp.post('/items/<int:media_item_id>/keep-forever')
    def keep_forever(media_item_id):
        return _perform(lambda: ProtectionEngine(get_db(), settings=get_settings()).keep_forever(
            _scope(media_item_id)), 'web.flash.keep_forever')

    @bp.post('/items/<int:media_item_id>/undo-keep-forever')
    def undo_keep_forever(media_item_id):
        return _perform(lambda: ProtectionEngine(get_db(), settings=get_settings()).undo_keep_forever(
            _scope(media_item_id)), 'web.flash.keep_forever_undone')

    @bp.post('/actions/scan')
    def run_scan():
        try:
            accepted = current_app.extensions['scan_runner'].start_background()
        except Exception as error:
            logger.warning('web_scan_start_failed', extra={'error_type': type(error).__name__})
            return _result('web.flash.action_failed')
        return _result('web.flash.scan_started' if accepted else 'web.flash.scan_running')

    @bp.post('/settings')
    def save():
        # The synced library list decides the S14 rule 7 refusal.
        libraries = tuple((row['plex_key'], row['title'])
                          for row in get_db().execute('SELECT plex_key, title FROM libraries'))
        settings, errors = validate_settings(get_settings(), request.form, libraries=libraries)
        if errors:
            return render_template('web/settings.html', model=settings_view(
                get_db(), get_settings(), errors=errors), submitted=request.form), 400
        if request.form.get('wizard_step') == 'libraries':
            # The first-run wizard continues with its next step.
            return _perform(lambda: save_settings(settings), 'web.flash.settings_saved',
                            'web.setup', step='connections')
        return _perform(lambda: save_settings(settings), 'web.flash.settings_saved', 'web.settings')

    @bp.post('/settings/kill-switch')
    def kill_switch():
        try:
            enabled = boolean(request.form.get('enabled'))
        except ValueError:
            return _result('web.flash.invalid_scope', 'web.settings')
        return _perform(lambda: set_kill_switch(get_db(), enabled),
                        'web.flash.kill_switch_on' if enabled else 'web.flash.kill_switch_off', 'web.settings')
