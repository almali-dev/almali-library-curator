"""POST-only owner actions. The lifecycle/protection engines own all SQL writes."""
import logging

from flask import current_app, flash, g, redirect, render_template, request, url_for

from almali_library_curator.config import save_settings
from almali_library_curator.detection.models import Candidate, Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.models import (
    DuplicateMarkError, GraceRangeError, ProtectedItemError, RequestStateError,
)
from almali_library_curator.lifecycle.safety import set_kill_switch
from almali_library_curator.plex import PlexClient
from almali_library_curator.protection import ProtectionEngine
from almali_library_curator.scan.pipeline import build_detection
from almali_library_curator.web.forms import boolean, validate_settings
from almali_library_curator.web.resources import get_db, get_settings
from almali_library_curator.web.views import now, settings_view

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


def _result(key, endpoint='web.library', **params):
    # Keys stay in the session; the shell translates at rendering time.
    flash(key)
    return redirect(url_for(endpoint, lang=g.get('locale', get_settings().locale), **params), code=303)


def _perform(action, success, endpoint='web.library', **params):
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
    except Exception as error:
        # A collection write may fail AFTER the durable engine transition.
        # Do not claim success, roll it back, or expose an external error body.
        get_db().rollback()
        logger.warning('web_action_failed', extra={'error_type': type(error).__name__})
        key = 'web.flash.action_failed'
    return _result(key, endpoint, **params)


def register_actions(bp):
    @bp.post('/items/<int:media_item_id>/mark')
    def mark(media_item_id):
        def action():
            candidate = _fresh_candidate(_scope(media_item_id))
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
        settings, errors = validate_settings(get_settings(), request.form)
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
