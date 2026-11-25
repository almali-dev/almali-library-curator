"""Read-only screen builders, usable without a Flask request or templates."""
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone, timedelta
import json
import logging
import math
from urllib.parse import urlsplit

from almali_library_curator import __version__, library_scope, secret_store
from almali_library_curator.config import PLEX_TOKEN_STORED, PLEX_TOKEN_UNREADABLE, SERVICE_KEY_FIELDS
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Unit
from almali_library_curator.i18n import DEFAULT_LOCALE, translate
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.deletion import shared_use_for_scope
from almali_library_curator.lifecycle.engine import cancel_reason_text
from almali_library_curator.lifecycle.safety import kill_switch_enabled
from almali_library_curator.plex import auth
from almali_library_curator.scan import last_scan
from almali_library_curator.scan import rows as scan_rows
from almali_library_curator.scan.snapshot import scope_storage
from almali_library_curator.web import format as fmt
from almali_library_curator.web.forms import SERVICE_URL_FIELDS
from almali_library_curator.web.models import (
    AboutLink, AboutView, ConnectionsView, EventView, GoLiveView, HISTORY_PAGE_SIZE, HistoryView,
    ItemView, ItemsView, LeavingItem, LeavingView, LIBRARY_MAX_PAGE_SIZE, LIBRARY_PAGE_SIZE,
    LibraryView, PlexFormView, PlexServerOptionView, PlexSignInView, ScanStatusView, ScanStepView,
    ServiceFormView, ServiceView, SettingsView, SetupChecklistView, SetupOptionView,
    SetupStepView, SourcesView, SummaryView, Totals,
)

logger = logging.getLogger(__name__)

# The published History columns, in order. The stored reason parts travel
# beside them, never as a column of their own: the CSV export keeps exactly
# this column list (#47). A failure's parameters (#136) are not a column
# either: the failure key is, rendered per screen.
EVENT_COLUMNS = tuple(
    field.name for field in fields(EventView) if field.name != 'error_params')
# ``reason_parts`` renders the stored reason per screen and ``media_item_id``
# names the other library of a sharing refusal (#136); neither is a column.
_EVENT_SELECT = ', '.join(EVENT_COLUMNS) + ', reason_parts, media_item_id'


def _stored_parts(raw):
    """The stored reason parts of a row, read defensively (#47).

    A row written before the parts existed, or one this version cannot
    read, answers nothing here and its stored text is shown instead (S1d).
    """
    if not raw:
        return ()
    try:
        parts = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parts, list):
        return ()
    return tuple(part for part in parts if isinstance(part, dict) and part.get('key'))


# The cancellation keys take raw parameters the detection renderer does not
# know (a user name, an ISO date), so they render through their own helper.
_CANCEL_PREFIX = 'removal.cancel.'


def _render_part(locale, part):
    """One stored reason part in ``locale``, by the renderer that owns it."""
    key = str(part['key'])
    if key.startswith(_CANCEL_PREFIX):
        return cancel_reason_text(locale, key[len(_CANCEL_PREFIX):], part.get('params') or {})
    return text.render_reason(locale, [part])


def render_stored_reason(locale, raw_parts, stored_text):
    """A stored reason in the reader's language, or its stored text (S1c/S1d)."""
    parts = _stored_parts(raw_parts)
    if not parts:
        return stored_text
    try:
        return ' '.join(_render_part(locale, part) for part in parts)
    except (KeyError, TypeError, ValueError, AttributeError):
        logger.warning('reason_parts_unreadable')
        return stored_text


def _cancel_reason(locale, request):
    """The cancellation reason in the reader's language (S1c), else its text.

    The reason is rebuilt from the stored code plus the raw parameters, so
    one cancellation reads in every language; a row written before the
    parameters existed has only its text, and keeps it (S1d).
    """
    if request.cancel_params is None or not request.recheck_status:
        return request.cancel_reason
    return cancel_reason_text(locale, request.recheck_status, request.cancel_params)


_SHARED_FAILURE_PREFIX = 'removal.failed.shared_'


def _shared_error_params(conn, locale, media_item_id, unit, seasons, error):
    """The other library a sharing refusal names, in the reader's language (#136).

    The stored failure is the translation key, never a rendered sentence, so
    the screen runs the same read-only sharing check again to name the other
    library. A name it cannot find still leaves a sentence: the generic
    phrase stands in for it.
    """
    if not error or not str(error).startswith(_SHARED_FAILURE_PREFIX) or unit is None:
        return {}
    conflict = None
    if media_item_id is not None:
        try:
            conflict = shared_use_for_scope(
                conn, media_item_id, unit, tuple(seasons or ()),
                # The stored code says which route refused: an arr entry is
                # only shared on the arr route, a file whatever the route.
                arr_route=str(error) == 'removal.failed.shared_arr_entry')
        except Exception:
            logger.warning('shared_use_lookup_failed', extra={'media_item_id': media_item_id})
    names = conflict.libraries if conflict is not None else ()
    library = ', '.join(names) if names else translate(
        'removal.failed.shared_other_library', locale)
    return {'library': library}


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

    The Library page reads the materialized scan rows, not the display
    snapshot (#48): the snapshot is parsed only by the accessor that hands
    out a whole report section, so a page load never decodes it.
    """

    def __init__(self, conn, settings=None):
        self.conn = conn
        self.settings = settings
        self.run = last_scan(conn)
        self._report = _UNSET
        self._curated = _UNSET

    @property
    def report(self):
        """The display sections of the run, decoded on first use only."""
        if self._report is _UNSET:
            self._report = self._sections(
                self.conn, self.run['id'] if self.run else None)
        return self._report

    def curated(self):
        """The media item ids inside the curated libraries, read once."""
        if self._curated is _UNSET:
            self._curated = (library_scope.curated_item_ids(self.conn, self.settings)
                             if self.settings is not None else None)
        return self._curated

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


def _would(run) -> bool:
    """True when the run the rows come from was a dry run."""
    return bool(run and run['mode'] == 'dry_run')


def _page_size(per_page) -> int:
    """The page size of a request, bounded so no URL can ask for everything."""
    try:
        size = int(per_page)
    except (TypeError, ValueError):
        return LIBRARY_PAGE_SIZE
    return max(1, min(size, LIBRARY_MAX_PAGE_SIZE))


def _item(row):
    """One cache row as the view model the page renders (#48)."""
    try:
        return ItemView(**row)
    except (KeyError, TypeError, ValueError):
        logger.warning('scan_display_row_unreadable')
        return None


def _page_view(conn, kind, *, category=None, page=1, per_page=LIBRARY_PAGE_SIZE,
               settings=None, facts=None) -> ItemsView:
    """One page of one kind of scan row, counted and ordered in SQL."""
    facts = facts if facts is not None else _Page(conn, settings)
    size = _page_size(per_page)
    total = scan_rows.count_rows(conn, kind, category=category, settings=settings)[0]
    pages = max(1, -(-total // size))
    # A page past the end answers the last one, as History does.
    number = min(max(1, int(page or 1)), pages)
    rows = scan_rows.page_rows(conn, kind, category=category, limit=size,
                               offset=(number - 1) * size, settings=settings)
    items = tuple(item for item in (_item(row) for row in rows) if item is not None)
    return ItemsView(items, scan_rows.category_counts(conn, kind, settings=settings),
                     _would(facts.run), facts.run, number, size, pages, total,
                     number < pages, number > 1)


def _tab_view(kind, totals, facts, per_page) -> ItemsView:
    """The tab count of a tab this request does not build, without its rows."""
    total = totals.get(kind, (0, 0))[0]
    return ItemsView((), {'all': total} if total else {}, _would(facts.run),
                     facts.run, 1, _page_size(per_page), 1, total)


def report_section_rows(conn, key, settings=None):
    """The rows of one report section of the newest finished run.

    The public accessor of a scan report section: a section the run does not
    carry is an empty list.
    """
    return _Page(conn, settings).rows(key)


def candidates_view(conn, category: str | None = None, settings=None, facts=None, *,
                    page=1, per_page=LIBRARY_PAGE_SIZE) -> ItemsView:
    """One page of the candidates of the latest finished run, largest first."""
    return _page_view(conn, 'candidate', category=category, page=page,
                      per_page=per_page, settings=settings, facts=facts)


def protected_view(conn, settings=None, facts=None, *, page=1,
                   per_page=LIBRARY_PAGE_SIZE) -> ItemsView:
    """One page of the protected items of the latest finished run."""
    return _page_view(conn, 'protected', page=page, per_page=per_page,
                      settings=settings, facts=facts)


def _scope_files(conn, media_item_id, unit, seasons) -> int:
    """How many files a removal of this scope would delete (#64).

    A movie is one file; a series or a season run is one file per episode in
    scope. Proven-absent Plex rows are already outside the curated scope.
    """
    item = conn.execute('SELECT plex_rating_key, file_path, file_size_bytes, plex_present'
                        ' FROM media_items WHERE id=?', (media_item_id,)).fetchone()
    if item is None or not item['plex_present']:
        return 0
    if unit == Unit.MOVIE:
        return 1 if item['file_path'] or item['file_size_bytes'] else 0
    rows = conn.execute('SELECT season_number FROM episodes'
                        ' WHERE series_rating_key=? AND plex_present=1',
                        (item['plex_rating_key'],)).fetchall()
    return sum(1 for row in rows
               if unit != Unit.SEASONS or row['season_number'] in seasons)


def _leaving_item(conn, request, clock, locale):
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
                       request.recheck_status, _cancel_reason(locale, request), request.last_error,
                       # #64: the date the files go, in the reader's own words, and
                       # how many files that is, so a row says what it will delete.
                       due_label=text.day_month_year(locale, request.due_at.astimezone()),
                       file_count=_scope_files(conn, request.media_item_id, request.unit,
                                               request.seasons),
                       # #136: a sharing refusal names the other library.
                       error_params=_shared_error_params(
                           conn, locale, request.media_item_id, request.unit,
                           request.seasons, request.last_error))


def _unreadable(row):
    return LeavingItem(row['id'], row['media_item_id'], row['title'], None, (), 0, None,
                       'unreadable', None, None, None, None, None, 'web.form.unreadable', True)


def leaving_soon_view(conn, settings, *, clock=now, facts=None, locale=None) -> LeavingView:
    engine = LifecycleEngine(conn, settings=settings, clock=clock)
    # The reader's language (a language switch or the setting), not the one
    # the request was marked in (#47).
    locale = locale or settings.locale
    facts = facts if facts is not None else _Page(conn, settings)
    # S14 rule 4: requests of deselected libraries stay in the database but
    # never reach the Leaving soon screen.
    allowed = facts.curated()
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
            open_items.append(
                _leaving_item(conn, engine._request_by_id(row['id']), clock, locale))
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
            cancelled.append(
                _leaving_item(conn, engine._request_by_id(row['id']), clock, locale))
        except Exception:
            cancelled.append(_unreadable(row))
    return LeavingView(open_items, tuple(cancelled))


def summary_view(conn, settings, *, clock=now, facts=None) -> SummaryView:
    facts = facts if facts is not None else _Page(conn, settings)
    leaving = leaving_soon_view(conn, settings, clock=clock, facts=facts)
    return _summary(conn, settings, scan_rows.totals(conn, settings=settings), leaving,
                    facts.run)


def go_live_view(conn, settings, *, clock=now) -> GoLiveView:
    """What turning dry run off starts: the items now due, and when (#64).

    Read before the setting is stored, so the confirmation the owner reads
    names exactly the removals that would run next.
    """
    leaving = leaving_soon_view(conn, settings, clock=clock)
    first_due = next((item.due_label for item in leaving.items if item.due_label), None)
    return GoLiveView(len(leaving.items), sum(item.size_bytes for item in leaving.items),
                      first_due, kill_switch_enabled(conn))


def _summary(conn, settings, totals, leaving, run):
    """The four summary cards, counted in SQL over the cached scan rows."""
    totals_of = lambda kind: Totals(totals.get(kind, (0, 0))[1], totals.get(kind, (0, 0))[0])
    deleted = conn.execute("SELECT COALESCE(SUM(freed_bytes),0), COUNT(*),"
                           " MIN(substr(occurred_at,1,7)) FROM removal_events WHERE outcome='deleted'").fetchone()
    return SummaryView(totals_of('candidate'), Totals(sum(row.size_bytes for row in leaving.items),
                                                      len(leaving.items)),
                       totals_of('protected'), settings.grace_days,
                       scan_rows.protection_kinds(conn, settings=settings),
                       deleted[0], deleted[1], deleted[2], run)


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


def _event_row(row, locale):
    """One History row with its reason in the reader's language (S1c/S1d)."""
    values = dict(row)
    values.pop('media_item_id', None)
    values['reason'] = render_stored_reason(
        locale, values.pop('reason_parts', None), values['reason'])
    return values


def _event_unit(value):
    """The stored unit as a ``Unit``, or ``None`` when it does not read."""
    try:
        return Unit(value)
    except ValueError:
        return None


def _event_view(conn, row, locale):
    """One History row, with the parameters of its stored failure key."""
    values = _event_row(row, locale)
    values['error_params'] = _shared_error_params(
        conn, locale, row['media_item_id'], _event_unit(row['unit']),
        # A History row carries no seasons; the refusal lookup falls back to
        # the series scope, which names the same sharing libraries.
        (), values.get('error'))
    return EventView(**values)


def history_view(conn, *, outcome: str | None = None, month: str | None = None,
                 page: int = 1, locale: str = DEFAULT_LOCALE) -> HistoryView:
    """The newest events, one page of them at a time (issue #9).

    The events are sorted by the instant they happened, and the index of
    the migration 8 serves that order, so the page reads only its rows.
    """
    where, params = _history_filters(outcome, month)
    total = int(conn.execute('SELECT COUNT(*) FROM removal_events' + where, params).fetchone()[0])
    page = max(1, int(page or 1))
    pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    # A page past the end answers the last one, and the offset stays
    # inside the range SQLite can hold.
    page = min(page, pages)
    events = tuple(_event_view(conn, row, locale) for row in conn.execute(
        f'SELECT {_EVENT_SELECT} FROM removal_events' + where
        + ' ORDER BY julianday(occurred_at) DESC, id DESC LIMIT ? OFFSET ?',
        [*params, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE]))
    monthly = {row[0]: row[1] for row in conn.execute(
        "SELECT substr(occurred_at,1,7) AS month, COALESCE(SUM(freed_bytes),0) FROM removal_events"
        " WHERE outcome='deleted' GROUP BY month ORDER BY month DESC")}
    return HistoryView(events, monthly, outcome, month, page, HISTORY_PAGE_SIZE,
                       page < pages, page > 1)


def history_rows(conn, *, outcome: str | None = None, month: str | None = None,
                 locale: str = DEFAULT_LOCALE):
    """Every filtered event, newest first, for the CSV export.

    The rows are read as they are written, so a long history never fills
    the memory of the export. Each row carries exactly the published
    columns, with its reason in ``locale`` (#47).
    """
    where, params = _history_filters(outcome, month)
    for row in conn.execute(
            f'SELECT {_EVENT_SELECT} FROM removal_events' + where
            + ' ORDER BY julianday(occurred_at) DESC, id DESC', params):
        yield _event_row(row, locale)


def _user_rows(conn):
    """Every Plex account the app knows, with whether it ever played (#122).

    Plex lists every account the server ever shared with, hundreds on a
    long-lived server, so the users the app shows are the ones with a
    recorded play in the Plex or Tautulli history.
    """
    return conn.execute(
        'SELECT plex_user_id, title, excluded,'
        ' EXISTS (SELECT 1 FROM play_events WHERE play_events.user_id = users.id) AS has_play'
        ' FROM users ORDER BY id')


def _excluded_names(settings):
    names = settings.excluded_users if settings is not None else ()
    return {name.strip().lower() for name in names if name.strip()}


def _is_excluded(row, excluded_names):
    return bool(row['excluded']) or (row['title'] or '').strip().lower() in excluded_names


def sources_view(conn, settings=None) -> SourcesView:
    """Facts of the curated scope (S14 rule 4); users and history stay
    server-level facts, so their counts do not follow the scope."""
    from almali_library_curator.enrichment.sources import cached_disks

    keys = library_scope.curated_library_keys(settings) if settings is not None else ()
    disks = cached_disks(conn, keys)
    if disks is None:
        disks = _source_disks(conn, keys)
    excluded = _excluded_names(settings)
    viewers = sum(1 for row in _user_rows(conn)
                  if row['has_play'] and not _is_excluded(row, excluded))
    return SourcesView(viewers,
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


# S7: the cards, in the order they are read, with the one line each service
# adds. The optional services reuse the checklist's copy (T1): the same words
# say the same thing on both screens.
CONNECTION_SERVICES = ('plex', 'tautulli', 'sonarr', 'radarr', 'tmdb', 'host')
CONNECTION_ADDS = {
    'plex': 'connections.plex.adds',
    'tautulli': 'setup.checklist.option.tautulli',
    'sonarr': 'setup.checklist.option.sonarr',
    'radarr': 'setup.checklist.option.radarr',
    'tmdb': 'setup.checklist.option.tmdb',
    'host': 'setup.checklist.option.host',
}
CONNECTION_BRAND_KEYS = {'host': 'connections.host'}


def _connection_facts(name, settings):
    """The values a card shows with no form to carry them (S7).

    The host paths are read from the configuration, so the card reports them
    instead of offering a form: the translation key and the raw value, ready
    to print.
    """
    if name != 'host':
        return ()
    return tuple((f'connections.host.{field}', value)
                 for field, value in (('media_root', settings.media_root),
                                      ('unraid_root', settings.unraid_root))
                 if value)


def connections_view(conn, settings, *, probes=None, probe=True,
                     open_service=None) -> ConnectionsView:
    """Service status; without ``probe`` only the configuration is read.

    Probing calls every configured service, so a plain page load shows
    'configured' and the owner asks for a live test explicitly. The services
    come in the order the Connections cards read them (S7): Plex, the one
    required connection, first, then the optional ones and the host paths.
    ``open_service`` is the card the server renders open: the one the owner
    asked for, or the one a refused save came from.
    """
    if probe and probes is None:
        probes = _probes(settings)
    configured = {name: bool(getattr(settings, name + '_base_url') and getattr(settings, credential))
                  for name, credential in (('plex', 'plex_token'), ('tautulli', 'tautulli_api_key'),
                                           ('sonarr', 'sonarr_api_key'), ('radarr', 'radarr_api_key'))}
    configured['tmdb'] = bool(settings.tmdb_api_token)
    configured['host'] = bool(settings.media_root or settings.unraid_root)
    services = []
    for name in CONNECTION_SERVICES:
        available = configured[name]
        status = 'not_configured'
        if available and not probe:
            status = 'configured'
        elif available:
            try:
                status = 'unreachable' if probes[name]() is False else 'connected'
            except Exception:
                status = 'unreachable'
                logger.warning('web_connection_unreachable', extra={'service': name})
        services.append(ServiceView(name, status, adds_key=CONNECTION_ADDS.get(name, ''),
                                    brand_key=CONNECTION_BRAND_KEYS.get(name),
                                    required=name == 'plex', open=name == open_service,
                                    facts=_connection_facts(name, settings)))
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
EDITABLE_SETTINGS = ('locale', 'grace_days', 'collection_name', 'minimum_rating', 'rating_protection_enabled',
                     'protected_libraries', 'curated_libraries', 'excluded_users', 'scan_time', 'dry_run',
                     'radarr_deletes', 'sonarr_deletes')


def plex_configured(settings) -> bool:
    """S15 rule 8: Plex is the one connection the app cannot work without."""
    return bool(settings.plex_base_url and settings.plex_token)


# The first-run checklist (S3): its five steps, the brands of the optional
# services and the one line each of them adds. The list is fixed, so the page
# always shows the same five numbered steps in the same order.
CHECKLIST_STEPS = 5
OPTIONAL_SERVICES = (('tautulli', 'Tautulli'), ('sonarr', 'Sonarr'),
                     ('radarr', 'Radarr'), ('tmdb', 'TMDB'))


def _plex_server(settings) -> str:
    """The Plex server the app is connected to, as the owner recognises it."""
    address = urlsplit(settings.plex_base_url)
    return address.netloc or settings.plex_base_url


def _optional_options(settings) -> tuple[SetupOptionView, ...]:
    """The optional connections, each with its own state and what it adds.

    A connection is configured when the app has both an address (or, for
    TMDB, nothing but its token) and the credential of that service: the
    same rule the Connections screen reads, so the two never disagree.
    """
    configured = {
        name: bool(getattr(settings, name + '_base_url') and getattr(settings, credential))
        for name, credential in (('tautulli', 'tautulli_api_key'), ('sonarr', 'sonarr_api_key'),
                                 ('radarr', 'radarr_api_key'))
    }
    configured['tmdb'] = bool(settings.tmdb_api_token)
    configured['host'] = bool(settings.media_root or settings.unraid_root)
    options = [SetupOptionView(name, brand, None,
                               'configured' if configured[name] else 'not_configured',
                               f'setup.checklist.option.{name}')
               for name, brand in OPTIONAL_SERVICES]
    options.append(SetupOptionView('host', None, 'connections.host',
                                   'configured' if configured['host'] else 'not_configured',
                                   'setup.checklist.option.host'))
    return tuple(options)


def _plex_step(number, settings) -> SetupStepView:
    """S3 rule 1: Plex, connected (with the server) or still to connect."""
    if plex_configured(settings):
        return SetupStepView(number, 'setup.step.plex', 'done', True,
                             value_key='setup.checklist.plex.done',
                             value_params={'server': _plex_server(settings)})
    return SetupStepView(number, 'setup.step.plex', 'pending', True,
                         value_key='setup.checklist.plex.missing',
                         action='plex', action_key='action.connect_plex')


def _analysis_step(number, settings, scan, run, locale) -> SetupStepView:
    """S3 rule 2: the first analysis, and the state it is really in.

    The recorded run decides: running wins (the owner just pressed the
    button), then a failure (with the step that failed, never the
    diagnostic), then the finished date of the newest finished run.
    """
    if scan is not None and scan.state == 'running':
        return SetupStepView(number, 'setup.step.analysis', 'running', True,
                             value_key='setup.checklist.analysis.running')
    if scan is not None and scan.state in ('failed', 'skipped'):
        failed = next((step.name for step in scan.steps if step.status == 'failed'), None)
        failed_key = ('setup.checklist.analysis.failed_step' if failed
                      else 'setup.checklist.analysis.failed')
        return SetupStepView(number, 'setup.step.analysis', 'failed', True,
                             value_key=failed_key,
                             value_params={'step': translate('scan.step.' + failed, locale)} if failed else {},
                             note_key='setup.checklist.analysis.next',
                             note_params={'time': settings.scan_time},
                             action='scan', action_key='action.scan_now')
    if run is not None:
        when = fmt.scan_label(run, locale, datetime.now().astimezone())
        return SetupStepView(number, 'setup.step.analysis', 'done', True,
                             value_key='setup.checklist.analysis.finished',
                             value_params={'when': when})
    return SetupStepView(number, 'setup.step.analysis', 'pending', True,
                         value_key='setup.checklist.analysis.pending',
                         note_key='setup.checklist.analysis.next',
                         note_params={'time': settings.scan_time},
                         action='scan', action_key='action.scan_now')


def setup_checklist_view(conn, settings, *, locale=DEFAULT_LOCALE, scan=None,
                         last_scan_run=None) -> SetupChecklistView:
    """The first-run checklist of the home page (S3).

    Shown while a required step is open (Plex not configured, or no finished
    analysis) and gone once both are done; the optional steps never keep it
    on screen. The view names each screen symbolically: the template builds
    the URL, so this stays a read model.
    """
    run = last_scan_run if last_scan_run is not None else last_scan(conn)
    libraries = conn.execute('SELECT COUNT(*) FROM libraries').fetchone()[0]
    options = _optional_options(settings)
    steps = (
        _plex_step(1, settings),
        _analysis_step(2, settings, scan, run, locale),
        # The optional steps say their own state line by line (which libraries
        # are managed, which services are configured) and stay optional here:
        # the artboard marks them all the same way, and none of them keeps
        # the checklist on screen.
        SetupStepView(3, 'setup.step.libraries', 'optional', False,
                      value_key=('setup.checklist.libraries.known' if libraries
                                 else 'setup.checklist.libraries.pending'),
                      value_params={'count': libraries} if libraries else {},
                      action='libraries', action_key='action.setup_libraries'),
        SetupStepView(4, 'setup.step.connections', 'optional', False,
                      value_key='setup.checklist.connections.intro',
                      action='connections', action_key='action.setup_connections',
                      options=options,
                      # S7: the link opens the first connection still missing,
                      # so the form the step names is on screen at once.
                      action_service=next((option.name for option in options
                                           if option.state != 'configured'), None)),
        SetupStepView(5, 'setup.step.go_live', 'optional', False,
                      value_key=('setup.checklist.go_live.dry_run' if settings.dry_run
                                 else 'setup.checklist.go_live.real'),
                      action='settings', action_key='action.open_settings',
                      item_state='dry_run' if settings.dry_run else 'real_deletion'),
    )
    # The first step still open is where the checklist says to start.
    first_open = next((index for index, step in enumerate(steps) if step.state != 'done'), None)
    steps = tuple(replace(step, active=index == first_open)
                  for index, step in enumerate(steps))
    done = sum(1 for step in steps if step.state == 'done')
    plex_open = not plex_configured(settings)
    analysis_open = run is None
    remaining = {(True, True): 'setup.checklist.remaining.both',
                 (True, False): 'setup.checklist.remaining.plex',
                 (False, True): 'setup.checklist.remaining.analysis'}
    return SetupChecklistView(steps, done, CHECKLIST_STEPS,
                              remaining.get((plex_open, analysis_open)),
                              plex_open or analysis_open)


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


def _settings_users(conn, settings):
    """The users the exclusion list offers: those with a recorded play, and
    every excluded one, so an exclusion can always be undone (#122 S3)."""
    excluded = _excluded_names(settings)
    return tuple({key: row[key] for key in ('plex_user_id', 'title', 'excluded')}
                 for row in _user_rows(conn)
                 if row['has_play'] or _is_excluded(row, excluded))


def settings_view(conn, settings, *, errors=None) -> SettingsView:
    values = {name: getattr(settings, name) for name in EDITABLE_SETTINGS}
    values['thresholds'] = asdict(settings.thresholds)
    curated = library_scope.curated_library_keys(settings)
    libraries = tuple(
        {**dict(row), 'curated': library_scope.library_is_curated(row['plex_key'], curated)}
        for row in conn.execute('SELECT plex_key, title, protected FROM libraries'))
    return SettingsView(values, kill_switch_enabled(conn),
                        libraries,
                        _settings_users(conn, settings),
                        errors or {})


def library_view(conn, settings, *, tab='candidates', category=None, clock=now,
                 locale=None, page=1, per_page=LIBRARY_PAGE_SIZE) -> LibraryView:
    """The Library screen, with only the tab the owner asked for built (#48).

    The candidate and protected tables read one page of the cached scan rows
    each; the other tab is a count, never a second page. Leaving soon keeps
    its own lifecycle read, because its rows come from the requests.
    """
    facts = _Page(conn, settings)
    totals = scan_rows.totals(conn, settings=settings)
    candidates = protected = None
    if tab == 'candidates':
        candidates = _page_view(conn, 'candidate', category=category, page=page,
                                per_page=per_page, settings=settings, facts=facts)
    if tab == 'protected':
        protected = _page_view(conn, 'protected', page=page, per_page=per_page,
                               settings=settings, facts=facts)
    candidates = candidates or _tab_view('candidate', totals, facts, per_page)
    protected = protected or _tab_view('protected', totals, facts, per_page)
    leaving = leaving_soon_view(conn, settings, clock=clock, facts=facts, locale=locale)
    return LibraryView(_summary(conn, settings, totals, leaving, facts.run),
                       candidates, protected, leaving,
                       sources_view(conn, settings), tab, redownload_warnings(settings),
                       dry_run=settings.dry_run, kill_switch=kill_switch_enabled(conn))
