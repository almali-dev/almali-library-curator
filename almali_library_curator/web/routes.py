"""Screen routes and CSV export over the typed view models."""
import csv
from dataclasses import asdict, fields
from datetime import datetime
import io

from flask import Blueprint, Response, g, render_template, request

from almali_library_curator.scan import last_scan
from almali_library_curator.web import actions as web_actions
from almali_library_curator.web import format as fmt
from almali_library_curator.web.models import EventView
from almali_library_curator.web.resources import get_db, get_settings
from almali_library_curator.web.views import (
    connections_view, history_view, library_view, plex_form_view, plex_signin_view, settings_view,
    sources_view,
)

bp = Blueprint('web', __name__, template_folder='templates')

LIBRARY_TABS = ('candidates', 'leaving_soon', 'protected')
WIZARD_STEPS = ('plex', 'libraries', 'connections')


def _locale():
    return g.get('locale', get_settings().locale)


@bp.app_template_filter('size')
def _size_filter(value):
    return fmt.human_size(value, _locale())


@bp.app_template_filter('month')
def _month_filter(value):
    return fmt.month_label(value, _locale())


@bp.context_processor
def _shell():
    settings = get_settings()
    conn = get_db()
    return {
        'sources': sources_view(conn, settings),
        'configured': {name: bool(getattr(settings, name + '_base_url') and getattr(settings, key))
                       for name, key in (('sonarr', 'sonarr_api_key'), ('radarr', 'radarr_api_key'))},
        'unraid': bool(settings.unraid_root),
        # Local wall-clock time, as the owner reads the schedule ("today 06:00").
        'last_scan_label': fmt.scan_label(last_scan(conn), _locale(), datetime.now().astimezone()),
        'plural': fmt.plural,
    }


def _scope_key(media_item_id, unit, seasons):
    return (media_item_id, unit, tuple(sorted(seasons)))


@bp.get('/')
def library():
    tab = request.args.get('tab', 'candidates')
    tab = tab if tab in LIBRARY_TABS else 'candidates'
    category = request.args.get('category') or 'all'
    model = library_view(get_db(), get_settings(), tab=tab, category=category)
    # Which candidate scopes already have an open removal (Mark -> Undo).
    open_requests = {_scope_key(item.media_item_id, item.unit, item.seasons): item.id
                     for item in model.leaving_soon.items if not item.unreadable}
    return render_template('web/library.html', model=model, category=category,
                           open_requests=open_requests, scope_key=_scope_key)


@bp.get('/history')
def history():
    model = history_view(get_db(), outcome=request.args.get('outcome') or None,
                         month=request.args.get('month') or None)
    return render_template('web/history.html', model=model)


@bp.get('/connections')
def connections():
    probe = request.args.get('test') == '1'
    current = get_settings()
    signin = plex_signin_view(web_actions.take_signin_state(current.config_dir))
    return render_template('web/connections.html',
                           model=connections_view(get_db(), current, probe=probe), tested=probe,
                           plex=plex_form_view(current, signin=signin))


@bp.get('/settings')
def settings():
    return render_template('web/settings.html', model=settings_view(get_db(), get_settings()))


@bp.get('/setup')
def setup():
    step = request.args.get('step', WIZARD_STEPS[0])
    step = step if step in WIZARD_STEPS else WIZARD_STEPS[0]
    conn, current = get_db(), get_settings()
    probe = step != 'libraries' and request.args.get('test') == '1'
    # The sign-in outcome is consumed where it is shown: the Plex step.
    signin = plex_signin_view(web_actions.take_signin_state(current.config_dir)) \
        if step == 'plex' else None
    return render_template('web/setup.html', step=step, steps=WIZARD_STEPS,
                           connections=connections_view(conn, current, probe=probe),
                           model=settings_view(conn, current), tested=probe,
                           plex=plex_form_view(current, signin=signin))


def _csv_cell(value):
    # Spreadsheet formulas are executable content, even when CSV-quoted.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


@bp.get('/history.csv')
def history_csv():
    model = history_view(get_db(), outcome=request.args.get('outcome') or None,
                         month=request.args.get('month') or None)
    output = io.StringIO(newline='')
    names = [field.name for field in fields(EventView)]
    writer = csv.DictWriter(output, fieldnames=names)
    writer.writeheader()
    for event in model.events:
        writer.writerow({key: _csv_cell(value) for key, value in asdict(event).items()})
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="history.csv"'})


from almali_library_curator.web.actions import register_actions  # noqa: E402
register_actions(bp)
