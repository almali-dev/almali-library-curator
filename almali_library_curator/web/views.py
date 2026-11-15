"""Read-only screen builders, usable without a Flask request or templates."""
from dataclasses import asdict, fields
from datetime import datetime, timezone, timedelta
import json
import logging
import math

from almali_library_curator import __version__, library_scope, secret_store
from almali_library_curator.config import PLEX_TOKEN_STORED, PLEX_TOKEN_UNREADABLE, SERVICE_KEY_FIELDS
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Category, Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.safety import kill_switch_enabled
from almali_library_curator.plex import auth
from almali_library_curator.scan import last_scan
from almali_library_curator.scan.snapshot import scope_storage
from almali_library_curator.web.forms import SERVICE_URL_FIELDS
from almali_library_curator.web.models import (
    AboutLink, AboutView, ConnectionsView, EventView, HISTORY_PAGE_SIZE, HistoryView, ItemView,
    ItemsView, LeavingItem, LeavingView, LibraryView, PlexFormView, PlexServerOptionView,
    PlexSignInView, ScanStatusView, ScanStepView, ServiceFormView, ServiceView, SettingsView,
    SourcesView, SummaryView, Totals,
)

logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


# The scan pipeline's step order; the run records them in this order too.
SCAN_STEPS = ('sync', 'enrichment', 'detection', 'recheck', 'deletion')


def _scan_in_progress(config_dir) -> bool:
    """True while a scan holds the cross-worker lock, probed and released."""
    from almali_library_curator.scan.lock import ScanLock

    lock = ScanLock(config_dir)
    if lock.acquire():
        lock.release()
        return False
    return True


def scan_status_view(conn, config_dir=None) -> ScanStatusView:
    """The outcome of the newest recorded scan run, any status (S2).

    The recorded run is the only source: its steps and their statuses say
    what succeeded and what failed, so the page never shows a raw exception
    body or a bare class name. A scan that holds the lock right now wins
    over any recorded outcome: the owner pressed the button and must see
    that it is running.
    """
    if config_dir is not None and _scan_in_progress(config_dir):
        return ScanStatusView('running')
    row = conn.execute('SELECT status, finished_at, steps FROM scan_runs'
                       ' LEFT JOIN scan_run_views ON run_id = scan_runs.id'
                       ' ORDER BY scan_runs.id DESC LIMIT 1').fetchone()
    if row is None:
        return ScanStatusView('never')
    try:
        recorded = json.loads(row['steps'] or '{}')
    except (TypeError, ValueError):
        recorded = {}
    steps = tuple(ScanStepView(name, recorded.get(name) or 'pending')
                  for name in SCAN_STEPS)
    return ScanStatusView(row['status'], steps, row['finished_at'])


# The project addresses of the About page (S19 rule 1). They are plain links:
# the page never calls them.
REPOSITORY_URL = "https://github.com/almali-dev/almali-library-curator"
ABOUT_LINKS = (
    ('about.repository', REPOSITORY_URL),
    ('about.changelog', f'{REPOSITORY_URL}/blob/main/CHANGELOG.md'),
    ('about.security', f'{REPOSITORY_URL}/blob/main/SECURITY.md'),
    ('about.issues', f'{REPOSITORY_URL}/issues'),
)


def about_view() -> AboutView:
    """The About page facts: the running version and the project links.

    S19 rule 4: nothing is read from the database, no service is contacted
    and no setting or secret reaches this view.
    """
    return AboutView(__version__,
                     tuple(AboutLink(label_key, url) for label_key, url in ABOUT_LINKS))


# A page reads the same facts for several views; this marker tells an
# empty answer from an answer that is not read yet.
_UNSET = object()


class _Page:
    """The facts one page reads, read once for the whole page (issue #7).

    The Library page reads the materialized display snapshot, not the full
    scan report. The snapshot keeps every section of the run's detection
    counts, so no section the page used to read is lost.
    """

    def __init__(self, conn, settings=None):
        self.conn = conn
        self.settings = settings
        self.run = last_scan(conn)
        self.report = self._sections(conn, self.run['id'] if self.run else None)
        self._items: dict[str, ItemsView] = {}
        self._curated = _UNSET

    @staticmethod
    def _sections(conn, run_id):
        """Every display section of the materialized snapshot of one run."""
        if run_id is None:
            return {}
        row = conn.execute('SELECT snapshot FROM scan_run_views WHERE run_id = ?',
                            (run_id,)).fetchone()
        try:
            snapshot = json.loads(row['snapshot'] or '{}') if row else {}
        except (TypeError, ValueError, AttributeError):
            return {}
        if not isinstance(snapshot, dict):
            return {}
        counts = snapshot.get('counts')
        if isinstance(counts, dict):
            return counts
        # Schema v10 kept the row sections at the top level of the snapshot.
        return snapshot

    def rows(self, key):
        """The rows of one report section, or none when it is damaged.

        A missing or null section is an empty list, never None: every
        caller iterates or measures the result.
        """
        try:
            value = self.report.get(key)
        except (TypeError, ValueError, AttributeError):
            return []
        return value if isinstance(value, list) else []

    def curated(self):
        """The media item ids inside the curated libraries, read once."""
        if self._curated is _UNSET:
            self._curated = (library_scope.curated_item_ids(self.conn, self.settings)
                             if self.settings is not None else None)
        return self._curated

    def items(self, key):
        """The items of one report section, built once per page."""
        if key not in self._items:
            self._items[key] = _build_items(self.conn, key, self)
        return self._items[key]


def _build_items(conn, key, page):
    rows = page.rows(key)
    items = []
    names = {f.name for f in fields(ItemView)}
    for row in rows if isinstance(rows, list) else []:
        try:
            values = {key: value for key, value in row.items() if key in names}
            for field in ('categories', 'seasons', 'reasons'):
                values[field] = tuple(values[field])
            # #47: the parts are optional, so a row written before them loads.
            for field in ('reason_parts', 'protection_reason_parts'):
                if field in values:
                    values[field] = tuple(
                        part for part in values[field] or () if isinstance(part, dict))
            items.append(ItemView(**values))
        except (KeyError, TypeError, ValueError):
            logger.warning('scan_display_row_unreadable')
    if page.settings is not None:
        # S14 rule 4: items of deselected libraries stay stored but are never
        # shown, even in a scan report written while they were curated.
        allowed = page.curated()
        if allowed is not None:
            items = [item for item in items if item.media_item_id in allowed]
    counts = {'all': len(items), **{category.value: sum(category.value in row.categories for row in items)
                                   for category in Category}}
    ordered = tuple(sorted(items, key=lambda row: (-row.size_bytes, row.title, row.media_item_id)))
    return ItemsView(ordered, counts, bool(page.run and page.run['mode'] == 'dry_run'), page.run)


def _in_category(view, category):
    """The same items, limited to one category of the report."""
    if not category or category == 'all':
        return view
    selected = tuple(row for row in view.items if category in row.categories)
    return ItemsView(selected, view.category_counts, view.would, view.last_scan)


def report_section_rows(conn, key, settings=None):
    """The rows of one report section of the newest finished run.

    The public accessor of a scan report section: the page reads the same
    rows through it, and a section the run does not carry is an empty list.
    """
    return _Page(conn, settings).rows(key)


def candidates_view(conn, category: str | None = None, settings=None, page=None) -> ItemsView:
    page = page if page is not None else _Page(conn, settings)
    return _in_category(page.items('candidate_rows'), category)


def protected_view(conn, settings=None, page=None) -> ItemsView:
    page = page if page is not None else _Page(conn, settings)
    return page.items('protected_rows')


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


def leaving_soon_view(conn, settings, *, clock=now, page=None) -> LeavingView:
    engine = LifecycleEngine(conn, settings=settings, clock=clock)
    page = page if page is not None else _Page(conn, settings)
    # S14 rule 4: requests of deselected libraries stay in the database but
    # never reach the Leaving soon screen.
    allowed = page.curated()
    in_scope = lambda media_item_id: allowed is None or media_item_id in allowed
    # S16 rule 1: each request is decoded on its own, so one damaged scope
    # carrier is reported as unreadable instead of breaking the screen.
    open_items = []
    for row in conn.execute('SELECT rr.id, rr.media_item_id, mi.title FROM removal_requests rr'
                            ' JOIN media_items mi ON mi.id=rr.media_item_id'
                            " WHERE rr.state NOT IN ('cancelled', 'deleted')"
                            ' ORDER BY rr.due_at, rr.id'):
        if not in_scope(row['media_item_id']):
            continue
        try:
            open_items.append(_leaving_item(conn, engine._request_by_id(row['id']), clock))
        except Exception:
            logger.warning('web_request_unreadable', extra={'request_id': row['id']})
            open_items.append(_unreadable(row))
    open_items = tuple(open_items)
    cancelled = []
    # Recent means the last 30 days, bounded to 50 rows; History retains all.
    cancelled_sql = ('SELECT rr.id, rr.media_item_id, mi.title FROM removal_requests rr'
                     ' JOIN media_items mi ON mi.id=rr.media_item_id'
                     ' JOIN libraries l ON l.id = mi.library_id'
                     " WHERE rr.state='cancelled' AND julianday(rr.updated_at)>=julianday(?)")
    cancelled_params = [(clock() - timedelta(days=30)).isoformat()]
    curated_keys = library_scope.curated_library_keys(settings)
    if curated_keys:
        cancelled_sql += ' AND l.plex_key IN (%s)' % ', '.join('?' for _ in curated_keys)
        cancelled_params += list(curated_keys)
    rows = conn.execute(cancelled_sql + ' ORDER BY rr.updated_at DESC, rr.id DESC LIMIT 50',
                        cancelled_params)
    for row in rows:
        try:
            cancelled.append(_leaving_item(conn, engine._request_by_id(row['id']), clock))
        except Exception:
            cancelled.append(_unreadable(row))
    return LeavingView(open_items, tuple(cancelled))


def summary_view(conn, settings, *, clock=now, page=None) -> SummaryView:
    page = page if page is not None else _Page(conn, settings)
    candidates = page.items('candidate_rows')
    protected = page.items('protected_rows')
    leaving = leaving_soon_view(conn, settings, clock=clock, page=page)
    return _summary(conn, settings, candidates, protected, leaving)


def _summary(conn, settings, candidates, protected, leaving):
    totals = lambda rows: Totals(sum(row.size_bytes for row in rows), len(rows))
    deleted = conn.execute("SELECT COALESCE(SUM(freed_bytes),0), COUNT(*),"
                           " MIN(substr(occurred_at,1,7)) FROM removal_events WHERE outcome='deleted'").fetchone()
    return SummaryView(totals(candidates.items), totals(leaving.items), totals(protected.items),
                       settings.grace_days, tuple(sorted({row.protection_kind for row in protected.items
                                                         if row.protection_kind})),
                       deleted[0], deleted[1], deleted[2], candidates.last_scan)


def _history_filters(outcome: str | None, month: str | None):
    """The WHERE clause and the parameters of the History filters."""
    clauses, params = [], []
    if outcome:
        clauses.append('outcome=?')
        params.append(outcome)
    if month:
        clauses.append('substr(occurred_at,1,7)=?')
        params.append(month)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    return where, params


def history_view(conn, *, outcome: str | None = None, month: str | None = None,
                 page: int = 1) -> HistoryView:
    """The newest events, one page of them at a time (issue #9).

    The events are sorted by the instant they happened, and the index of
    the migration 8 serves that order, so the page reads only its rows.
    """
    where, params = _history_filters(outcome, month)
    columns = ', '.join(f.name for f in fields(EventView))
    total = int(conn.execute('SELECT COUNT(*) FROM removal_events' + where, params).fetchone()[0])
    page = max(1, int(page or 1))
    pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    # A page past the end answers the last one, and the offset stays
    # inside the range SQLite can hold.
    page = min(page, pages)
    events = tuple(EventView(**dict(row)) for row in conn.execute(
        f'SELECT {columns} FROM removal_events' + where
        + ' ORDER BY julianday(occurred_at) DESC, id DESC LIMIT ? OFFSET ?',
        [*params, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE]))
    monthly = {row[0]: row[1] for row in conn.execute(
        "SELECT substr(occurred_at,1,7) AS month, COALESCE(SUM(freed_bytes),0) FROM removal_events"
        " WHERE outcome='deleted' GROUP BY month ORDER BY month DESC")}
    return HistoryView(events, monthly, outcome, month, page, HISTORY_PAGE_SIZE,
                       page < pages, page > 1)


def history_rows(conn, *, outcome: str | None = None, month: str | None = None):
    """Every filtered event, newest first, for the CSV export.

    The rows are read as they are written, so a long history never fills
    the memory of the export.
    """
    where, params = _history_filters(outcome, month)
    columns = ', '.join(f.name for f in fields(EventView))
    return conn.execute(
        f'SELECT {columns} FROM removal_events' + where
        + ' ORDER BY julianday(occurred_at) DESC, id DESC', params)


def sources_view(conn, settings=None) -> SourcesView:
    """Facts of the curated scope (S14 rule 4); users and history stay
    server-level facts, so their counts do not follow the scope."""
    from almali_library_curator.enrichment.sources import cached_disks

    keys = library_scope.curated_library_keys(settings) if settings is not None else ()
    disks = cached_disks(conn, keys)
    if disks is None:
        disks = _source_disks(conn, keys)
    return SourcesView(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
                       conn.execute("SELECT MIN(played_at) FROM play_events WHERE source='tautulli'").fetchone()[0],
                       len(disks))


def _source_disks(conn, keys):
    """Pre-cache query for installs without a usable file-fact summary."""
    if keys:
        marks = ', '.join('?' for _ in keys)
        return {row[0] for row in conn.execute(
            'SELECT mi.disk FROM media_items mi JOIN libraries l ON l.id = mi.library_id'
            f' WHERE l.plex_key IN ({marks})'
            ' UNION SELECT e.disk FROM episodes e'
            ' JOIN media_items s ON s.plex_rating_key = e.series_rating_key'
            ' JOIN libraries l2 ON l2.id = s.library_id'
            f' WHERE l2.plex_key IN ({marks})', (*keys, *keys)) if row[0]}
    return {row[0] for row in conn.execute(
        'SELECT disk FROM media_items UNION SELECT disk FROM episodes') if row[0]}


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
    return ConnectionsView(tuple(services), sources_view(conn, settings))


def plex_signin_view(state, *, servers=()) -> PlexSignInView | None:
    """The pending sign-in panel from the state the actions layer wrote.

    ``state`` is the dict stored at ``<config_dir>/plex-signin.json`` or
    None. It holds the PIN code and translation keys only: the token never
    reaches this view or the page (S13 rules 5-6).
    """
    if not state:
        return None
    signin = state.get('state')
    if signin not in ('pending', 'authorized', 'succeeded', 'failed', 'cancelled'):
        return None
    pending = signin == 'pending'
    return PlexSignInView(
        state=signin,
        code=state.get('code') if pending else None,
        link=(auth.authorization_link(state.get('client_identifier', ''), state.get('code', ''))
              if pending else None),
        message=state.get('message'),
        servers=tuple(PlexServerOptionView(server.name, server.address) for server in servers),
    )


def plex_form_view(settings, *, errors=None, base_url=None, signin=None) -> PlexFormView:
    """The Plex form: the address, and only a masked token when one is stored."""
    stored = settings.plex_token_status == PLEX_TOKEN_STORED and bool(settings.plex_token)
    return PlexFormView(
        base_url=settings.plex_base_url if base_url is None else base_url,
        token_mask=secret_store.mask(settings.plex_token) if stored else None,
        env_managed=settings.plex_env_managed,
        token_unreadable=settings.plex_token_status == PLEX_TOKEN_UNREADABLE and not settings.plex_env_managed,
        errors=errors or {},
        signin=signin)


# The brands of the optional services (S20). They are proper names, the same
# in every language.
SERVICE_BRANDS = {'sonarr': 'Sonarr', 'radarr': 'Radarr', 'tautulli': 'Tautulli', 'tmdb': 'TMDB'}


def service_forms_view(settings, *, errors=None, service=None, base_url=None):
    """One key form per optional service (S20 rule 1).

    Only a masked key reaches the page, and only when the key comes from
    the encrypted store. ``errors`` are the ones of a refused submission:
    they belong to ``service`` alone. ``base_url`` is the address to show
    back to the owner after a refusal.
    """
    errors = errors or {}
    forms = []
    for name, key_field in SERVICE_KEY_FIELDS.items():
        url_field = SERVICE_URL_FIELDS.get(name)
        own = {}
        if service is None or service == name:
            for field, key in errors.items():
                if field in (key_field, url_field) or field == 'service':
                    own[field] = key
        stored = key_field in settings.stored_secrets
        address = getattr(settings, url_field, '') if url_field else ''
        if service == name and base_url is not None:
            address = base_url
        forms.append(ServiceFormView(
            name=name,
            brand=SERVICE_BRANDS[name],
            url_field=url_field,
            key_field=key_field,
            base_url=address,
            key_mask=secret_store.mask(getattr(settings, key_field)) if stored else None,
            env_managed=name in settings.env_managed_services,
            key_unreadable=key_field in settings.unreadable_secrets,
            errors=own))
    return tuple(forms)


# An allowlist, not Settings-asdict: adding a credential cannot expose it.
EDITABLE_SETTINGS = ('grace_days', 'collection_name', 'minimum_rating', 'rating_protection_enabled',
                     'protected_libraries', 'curated_libraries', 'excluded_users', 'scan_time', 'dry_run',
                     'radarr_deletes', 'sonarr_deletes')


def plex_configured(settings) -> bool:
    """S15 rule 8: Plex is the one connection the app cannot work without."""
    return bool(settings.plex_base_url and settings.plex_token)


def redownload_warnings(settings) -> dict:
    """S15 rule 5: unit types whose configured arr has its deletions off.

    Plex then removes those items, but it cannot unmonitor them or add an
    import exclusion, so that arr may download them again.
    """
    warnings = {}
    if settings.radarr_base_url and settings.radarr_api_key and not settings.radarr_deletes:
        warnings['movie'] = 'Radarr'
    if settings.sonarr_base_url and settings.sonarr_api_key and not settings.sonarr_deletes:
        warnings.update(series='Sonarr', seasons='Sonarr')
    return warnings


def settings_view(conn, settings, *, errors=None) -> SettingsView:
    values = {name: getattr(settings, name) for name in EDITABLE_SETTINGS}
    values['thresholds'] = asdict(settings.thresholds)
    curated = library_scope.curated_library_keys(settings)
    libraries = tuple(
        {**dict(row), 'curated': library_scope.library_is_curated(row['plex_key'], curated)}
        for row in conn.execute('SELECT plex_key, title, protected FROM libraries'))
    return SettingsView(values, kill_switch_enabled(conn),
                        libraries,
                        tuple(dict(row) for row in conn.execute('SELECT plex_user_id, title, excluded FROM users')),
                        errors or {})


def library_view(conn, settings, *, tab='candidates', category=None, clock=now) -> LibraryView:
    page = _Page(conn, settings)
    candidates = page.items('candidate_rows')
    protected = page.items('protected_rows')
    leaving = leaving_soon_view(conn, settings, clock=clock, page=page)
    return LibraryView(_summary(conn, settings, candidates, protected, leaving),
                       _in_category(candidates, category), protected, leaving,
                       sources_view(conn, settings), tab, redownload_warnings(settings),
                       dry_run=settings.dry_run, kill_switch=kill_switch_enabled(conn))
