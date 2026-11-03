"""Read-only screen builders, usable without a Flask request or templates."""
from dataclasses import asdict, fields
from datetime import datetime, timezone, timedelta
import json
import logging
import math

from almali_library_curator.detection import text
from almali_library_curator.detection.models import Category, Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.safety import kill_switch_enabled
from almali_library_curator.scan import last_scan
from almali_library_curator.scan.snapshot import scope_storage
from almali_library_curator.web.models import (
    ConnectionsView, EventView, HistoryView, ItemView, ItemsView, LeavingItem,
    LeavingView, LibraryView, ServiceView, SettingsView, SourcesView, SummaryView, Totals,
)

logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _items(conn, key, category=None):
    run = last_scan(conn)
    try:
        report = json.loads(run['report'] or '{}') if run else {}
        rows = report.get('detection', {}).get('counts', {}).get(key, [])
    except (TypeError, ValueError, AttributeError):
        rows = []
    items = []
    names = {f.name for f in fields(ItemView)}
    for row in rows if isinstance(rows, list) else []:
        try:
            values = {key: value for key, value in row.items() if key in names}
            for field in ('categories', 'seasons', 'reasons'):
                values[field] = tuple(values[field])
            items.append(ItemView(**values))
        except (KeyError, TypeError, ValueError):
            logger.warning('scan_display_row_unreadable')
    counts = {'all': len(items), **{category.value: sum(category.value in row.categories for row in items)
                                   for category in Category}}
    selected = (row for row in items if not category or category == 'all' or category in row.categories)
    ordered = tuple(sorted(selected, key=lambda row: (-row.size_bytes, row.title, row.media_item_id)))
    return ItemsView(ordered, counts, bool(run and run['mode'] == 'dry_run'), run)


def candidates_view(conn, category: str | None = None) -> ItemsView:
    return _items(conn, 'candidate_rows', category)


def protected_view(conn) -> ItemsView:
    return _items(conn, 'protected_rows')


def _leaving_item(conn, request, clock):
    item = conn.execute('SELECT title FROM media_items WHERE id=?', (request.media_item_id,)).fetchone()
    title = item['title'] if item else ''
    if request.unit == Unit.SEASONS:
        title += ' ' + text.season_runs(list(request.seasons))
    size, disk = scope_storage(conn, request.media_item_id, request.unit, request.seasons)
    remaining = max(0, math.ceil((request.due_at - clock()).total_seconds() / 86400))
    progress = max(0.0, min(1.0, 1 - (request.due_at - clock()).total_seconds() /
                                  (request.grace_days * 86400)))
    return LeavingItem(request.id, request.media_item_id, title, request.unit.value, request.seasons,
                       size, disk, request.state.value, remaining, request.grace_days, progress,
                       request.recheck_status, request.cancel_reason, request.last_error)


def _unreadable(row):
    return LeavingItem(row['id'], row['media_item_id'], row['title'], None, (), 0, None,
                       'unreadable', None, None, None, None, None, 'web.form.unreadable', True)


def leaving_soon_view(conn, settings, *, clock=now) -> LeavingView:
    engine = LifecycleEngine(conn, settings=settings, clock=clock)
    try:
        open_items = tuple(_leaving_item(conn, request, clock) for request in engine.open_requests())
    except Exception:
        # The engine's public bulk reader is deliberately strict. Isolate a
        # damaged carrier using its existing single-request decoder instead.
        open_items = []
        rows = conn.execute('SELECT rr.id, rr.media_item_id, mi.title FROM removal_requests rr'
                            ' JOIN media_items mi ON mi.id=rr.media_item_id'
                            " WHERE rr.state NOT IN ('cancelled', 'deleted') ORDER BY rr.due_at, rr.id")
        for row in rows:
            try:
                open_items.append(_leaving_item(conn, engine._request_by_id(row['id']), clock))
            except Exception:
                logger.warning('web_request_unreadable', extra={'request_id': row['id']})
                open_items.append(_unreadable(row))
        open_items = tuple(open_items)
    cancelled = []
    # Recent means the last 30 days, bounded to 50 rows; History retains all.
    rows = conn.execute('SELECT rr.id, rr.media_item_id, mi.title FROM removal_requests rr'
                        ' JOIN media_items mi ON mi.id=rr.media_item_id'
                        " WHERE rr.state='cancelled' AND julianday(rr.updated_at)>=julianday(?)"
                        ' ORDER BY rr.updated_at DESC, rr.id DESC LIMIT 50',
                        ((clock() - timedelta(days=30)).isoformat(),))
    for row in rows:
        try:
            cancelled.append(_leaving_item(conn, engine._request_by_id(row['id']), clock))
        except Exception:
            cancelled.append(_unreadable(row))
    return LeavingView(open_items, tuple(cancelled))


def summary_view(conn, settings, *, clock=now) -> SummaryView:
    candidates = candidates_view(conn)
    protected = protected_view(conn)
    leaving = leaving_soon_view(conn, settings, clock=clock)
    totals = lambda rows: Totals(sum(row.size_bytes for row in rows), len(rows))
    deleted = conn.execute("SELECT COALESCE(SUM(freed_bytes),0), COUNT(*),"
                           " MIN(substr(occurred_at,1,7)) FROM removal_events WHERE outcome='deleted'").fetchone()
    return SummaryView(totals(candidates.items), totals(leaving.items), totals(protected.items),
                       settings.grace_days, tuple(sorted({row.protection_kind for row in protected.items
                                                         if row.protection_kind})),
                       deleted[0], deleted[1], deleted[2], candidates.last_scan)


def history_view(conn, *, outcome: str | None = None, month: str | None = None) -> HistoryView:
    clauses, params = [], []
    if outcome:
        clauses.append('outcome=?')
        params.append(outcome)
    if month:
        clauses.append('substr(occurred_at,1,7)=?')
        params.append(month)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    columns = ', '.join(f.name for f in fields(EventView))
    events = tuple(EventView(**dict(row)) for row in conn.execute(
        f'SELECT {columns} FROM removal_events' + where + ' ORDER BY julianday(occurred_at) DESC, id DESC', params))
    monthly = {row[0]: row[1] for row in conn.execute(
        "SELECT substr(occurred_at,1,7) AS month, COALESCE(SUM(freed_bytes),0) FROM removal_events"
        " WHERE outcome='deleted' GROUP BY month ORDER BY month DESC")}
    return HistoryView(events, monthly, outcome, month)


def sources_view(conn) -> SourcesView:
    disks = {row[0] for row in conn.execute('SELECT disk FROM media_items UNION SELECT disk FROM episodes') if row[0]}
    return SourcesView(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
                       conn.execute("SELECT MIN(played_at) FROM play_events WHERE source='tautulli'").fetchone()[0],
                       len(disks))


def _probes(settings):
    # Construct clients inside each probe: invalid configuration is isolated too.
    from almali_library_curator.plex import PlexClient
    from almali_library_curator.sonarr import SonarrClient
    from almali_library_curator.radarr import RadarrClient
    from almali_library_curator.tautulli import TautulliClient
    from almali_library_curator.tmdb import TmdbClient
    from almali_library_curator.hostfs import HostFilesystem

    def host():
        fs = HostFilesystem.from_settings(settings)
        return all(fs.is_mounted(path) for path in (settings.media_root, settings.unraid_root) if path)

    return {'plex': lambda: PlexClient.from_settings(settings).sections(),
            'sonarr': lambda: SonarrClient.from_settings(settings).system_status(),
            'radarr': lambda: RadarrClient.from_settings(settings).system_status(),
            'tautulli': lambda: TautulliClient.from_settings(settings).users(),
            'tmdb': lambda: TmdbClient.from_settings(settings).movie(550), 'host': host}


def connections_view(conn, settings, *, probes=None, probe=True) -> ConnectionsView:
    """Service status; without ``probe`` only the configuration is read.

    Probing calls every configured service, so a plain page load shows
    'configured' and the owner asks for a live test explicitly.
    """
    if probe and probes is None:
        probes = _probes(settings)
    configured = {name: bool(getattr(settings, name + '_base_url') and getattr(settings, credential))
                  for name, credential in (('plex', 'plex_token'), ('sonarr', 'sonarr_api_key'),
                                           ('radarr', 'radarr_api_key'), ('tautulli', 'tautulli_api_key'))}
    configured.update(tmdb=bool(settings.tmdb_api_token), host=bool(settings.media_root or settings.unraid_root))
    services = []
    for name, available in configured.items():
        status = 'not_configured'
        if available and not probe:
            status = 'configured'
        elif available:
            try:
                status = 'unreachable' if probes[name]() is False else 'connected'
            except Exception:
                status = 'unreachable'
                logger.warning('web_connection_unreachable', extra={'service': name})
        services.append(ServiceView(name, status))
    return ConnectionsView(tuple(services), sources_view(conn))


# An allowlist, not Settings-asdict: adding a credential cannot expose it.
EDITABLE_SETTINGS = ('grace_days', 'collection_name', 'minimum_rating', 'rating_protection_enabled',
                     'protected_libraries', 'excluded_users', 'scan_time', 'dry_run')


def settings_view(conn, settings, *, errors=None) -> SettingsView:
    values = {name: getattr(settings, name) for name in EDITABLE_SETTINGS}
    values['thresholds'] = asdict(settings.thresholds)
    return SettingsView(values, kill_switch_enabled(conn),
                        tuple(dict(row) for row in conn.execute('SELECT plex_key, title, protected FROM libraries')),
                        tuple(dict(row) for row in conn.execute('SELECT plex_user_id, title, excluded FROM users')),
                        errors or {})


def library_view(conn, settings, *, tab='candidates', category=None, clock=now) -> LibraryView:
    return LibraryView(summary_view(conn, settings, clock=clock), candidates_view(conn, category),
                       protected_view(conn), leaving_soon_view(conn, settings, clock=clock),
                       sources_view(conn), tab)
