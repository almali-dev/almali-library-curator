"""Read-only screen builders, usable without a Flask request or templates."""
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
from urllib.parse import urlsplit

from flask import g, has_request_context

from almali_library_curator import __version__, library_scope, secret_store
from almali_library_curator.config import PLEX_TOKEN_STORED, PLEX_TOKEN_UNREADABLE, SERVICE_KEY_FIELDS
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Unit
from almali_library_curator.i18n import DEFAULT_LOCALE, translate
from almali_library_curator.imdb import imdb_url
from almali_library_curator.rottentomatoes import rt_url
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.deletion import shared_use_for_scope
from almali_library_curator.lifecycle.engine import cancel_reason_text
from almali_library_curator.lifecycle.safety import kill_switch_enabled
from almali_library_curator.plex import auth
from almali_library_curator.scan import last_scan
from almali_library_curator.scan import rows as scan_rows
from almali_library_curator.scan.snapshot import disk_labels
from almali_library_curator.web import format as fmt
from almali_library_curator.web.forms import SERVICE_URL_FIELDS
from almali_library_curator.web.models import (
    AboutLink, AboutView, ConnectionsView, DUPLICATES_GROUP_LIMIT, DuplicateCopy,
    DuplicateGroup, DuplicatesView, EventView, GoLiveView, HISTORY_PAGE_SIZE, HistoryView,
    ItemView, ItemsView, LEAVING_DEFAULT_SORT, LEAVING_SORTS, LeavingItem, LeavingView,
    LIBRARY_MAX_PAGE_SIZE, LIBRARY_PAGE_SIZE, LibraryView, PlexFormView, PlexServerOptionView,
    PlexSignInView, ScanStatusView, ScanStepView, ServiceFormView, ServiceView, SettingsView,
    SetupChecklistView, SetupOptionView, SetupStepView, SourcesView, SummaryView, Totals,
)

logger = logging.getLogger(__name__)

# The published History columns, in order. The stored reason parts travel
# beside them, never as a column of their own: the CSV export keeps exactly
# this column list (#47). A failure's parameters (#136) are not a column
# either: the failure key is, rendered per screen.
# #141: the item id and thumb feed the History card's poster; like the
# failure parameters they are screen-only, never a published CSV column.
# #153: the IMDb address is screen-only too, for the same reason.
_SCREEN_ONLY_EVENT_FIELDS = frozenset({'error_params', 'media_item_id', 'thumb', 'imdb_url',
                                       'imdb_rating', 'rt_critic', 'rt_url'})
EVENT_COLUMNS = tuple(
    field.name for field in fields(EventView) if field.name not in _SCREEN_ONLY_EVENT_FIELDS)
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
               settings=None, facts=None, sort=None, scope=None) -> ItemsView:
    """One page of one kind of scan row, counted and ordered in SQL."""
    facts = facts if facts is not None else _Page(conn, settings)
    scope = scope if scope is not None else scan_rows.Scope(conn, settings)
    size = _page_size(per_page)
    chosen = scan_rows.active_sort(sort)
    total = scan_rows.count_rows(conn, kind, category=category, settings=settings,
                                 scope=scope)[0]
    pages = max(1, -(-total // size))
    # A page past the end answers the last one, as History does.
    number = min(max(1, int(page or 1)), pages)
    rows = scan_rows.page_rows(conn, kind, category=category, limit=size,
                               offset=(number - 1) * size, settings=settings,
                               sort=chosen, scope=scope)
    items = tuple(item for item in (_item(row) for row in rows) if item is not None)
    return ItemsView(items, scan_rows.category_counts(conn, kind, settings=settings,
                                                      scope=scope),
                     _would(facts.run), facts.run, number, size, pages, total,
                     number < pages, number > 1, chosen)


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
                    page=1, per_page=LIBRARY_PAGE_SIZE, sort=None) -> ItemsView:
    """One page of the candidates of the latest finished run, largest first."""
    return _page_view(conn, 'candidate', category=category, page=page,
                      per_page=per_page, settings=settings, facts=facts, sort=sort)


def protected_view(conn, settings=None, facts=None, *, page=1,
                   per_page=LIBRARY_PAGE_SIZE, sort=None) -> ItemsView:
    """One page of the protected items of the latest finished run."""
    return _page_view(conn, 'protected', page=page, per_page=per_page,
                      settings=settings, facts=facts, sort=sort)


# S1 (#148): one statement reads every request of the screen — its own
# columns, the scope carrier it decodes and the item facts its row shows —
# and one grouped statement reads the episodes of every series on it. The
# item's own timestamp columns stay out: ``rr.*`` already carries its own.
_LEAVING_ROW_COLUMNS = (
    "mi.title AS title, mi.year, mi.imdb_id, mi.added_at, mi.rating,"
    " mi.last_played_at, mi.thumb, mi.plex_rating_key, mi.file_path,"
    " mi.file_size_bytes, mi.disk, mi.plex_present, mi.imdb_rating, mi.rt_critic"
)
_LEAVING_SELECT = (
    f"SELECT rr.*, c.facts AS scope_facts, {_LEAVING_ROW_COLUMNS}"
    " FROM removal_requests rr"
    " LEFT JOIN candidates c ON c.id = rr.candidate_id"
    " JOIN media_items mi ON mi.id = rr.media_item_id"
)


def _scope_episodes(conn, series_keys) -> dict[str, dict[int, tuple[int, int, frozenset]]]:
    """The present episodes of those series, grouped per season, in one read.

    Each season answers its file count, its size and the disks its episodes
    live on, which is every fact the size, the disk label and the file count
    of a series or seasons row are built from (#64, S1). Proven-absent
    episodes stay outside the scope, exactly as they always were.
    """
    found: dict[str, dict[int, tuple[int, int, frozenset]]] = {}
    for chunk in _chunks(sorted(key for key in series_keys if key)):
        marks = ', '.join('?' for _ in chunk)
        for row in conn.execute(
                'SELECT series_rating_key, season_number, COUNT(*) AS files,'
                ' COALESCE(SUM(file_size_bytes), 0) AS size_bytes,'
                ' json_group_array(DISTINCT disk) AS disks'
                ' FROM episodes WHERE plex_present = 1'
                f' AND series_rating_key IN ({marks})'
                ' GROUP BY series_rating_key, season_number', chunk):
            found.setdefault(row['series_rating_key'], {})[row['season_number']] = (
                int(row['files']), int(row['size_bytes']), disk_labels(row['disks']))
    return found


def _scope_numbers(item, unit, seasons, episodes) -> tuple[int, str | None, int]:
    """Size, disks and file count of one removal's scope (#64, S1).

    A movie is its own file; a series or a season run is one file per present
    episode in scope. ``episodes`` is the one grouped read of the page, so a
    row never asks the database anything of its own.
    """
    if item is None or not item['plex_present']:
        return 0, None, 0
    if unit == Unit.MOVIE:
        return ((item['file_size_bytes'] or 0), item['disk'],
                1 if item['file_path'] or item['file_size_bytes'] else 0)
    per_season = episodes.get(item['plex_rating_key'], {})
    wanted = [number for number in per_season
              if unit != Unit.SEASONS or number in seasons]
    disks = sorted({label for number in wanted for label in per_season[number][2]})
    return (sum(per_season[number][1] for number in wanted),
            ', '.join(disks) or None,
            sum(per_season[number][0] for number in wanted))


def _leaving_item(conn, request, clock, locale, *, item, episodes):
    title = (item['title'] or '') if item else ''
    if request.unit == Unit.SEASONS:
        title += ' ' + text.season_runs(list(request.seasons))
    size, disk, file_count = _scope_numbers(item, request.unit, request.seasons, episodes)
    remaining = max(0, math.ceil((request.due_at - clock()).total_seconds() / 86400))
    progress = max(0.0, min(1.0, 1 - (request.due_at - clock()).total_seconds() /
                                  (request.grace_days * 86400)))
    return LeavingItem(request.id, request.media_item_id, title, request.unit.value, request.seasons,
                       size, disk, request.state.value, remaining, request.grace_days, progress,
                       request.recheck_status, _cancel_reason(locale, request), request.last_error,
                       # #64: the date the files go, in the reader's own words, and
                       # how many files that is, so a row says what it will delete.
                       due_label=text.day_month_year(locale, request.due_at.astimezone()),
                       file_count=file_count,
                       # #136: a sharing refusal names the other library.
                       error_params=_shared_error_params(
                           conn, locale, request.media_item_id, request.unit,
                           request.seasons, request.last_error),
                       # #134: the facts its own sort orders read, from the item.
                       added_at=item['added_at'] if item else None,
                       rating=item['rating'] if item else None,
                       last_played_at=item['last_played_at'] if item else None,
                       # #139: the poster of the item the request belongs to.
                       thumb=item['thumb'] if item else None,
                       # #153: the item's IMDb link. It is built before the
                       # seasons suffix is shown, so a seasons request searches
                       # for its series, never for "1923 S1".
                       imdb_url=(imdb_url(item['imdb_id'], item['title'], item['year'])
                                 if item else None),
                       # #162: the scores Plex sent and the Rotten Tomatoes search.
                       imdb_rating=item['imdb_rating'] if item else None,
                       rt_critic=item['rt_critic'] if item else None,
                       rt_url=rt_url(item['title']) if item else None)


def _unreadable(row):
    return LeavingItem(row['id'], row['media_item_id'], row['title'], None, (), 0, None,
                       'unreadable', None, None, None, None, None, 'web.form.unreadable', True)


def _moment(value) -> float | None:
    """An ISO instant as a number two rows can be compared on, or None."""
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.timestamp()


def _leaving_sort_key(item, sort):
    """What one Leaving soon row sorts on, and where a missing value goes.

    A row the screen could not read at all has no facts to order: it follows
    the others (#16). Age and rating put the missing value last, exactly as
    Candidates and Protected do; time without plays uses the detection
    engine's reference (the last play, else the added date) and puts a
    never-played or unknown row first, so a missing instant never passes as
    a long time without plays.
    """
    title = (0, item.title, item.media_item_id, item.id)
    if item.unreadable:
        return (1, 0, 0.0, *title)
    if sort == 'size':
        return (0, 0, -item.size_bytes, *title)
    if sort == 'age':
        added = _moment(item.added_at)
        return (0, added is None, 0.0 if added is None else added, *title)
    if sort == 'rating':
        rating = item.rating
        return (0, rating is None, 0.0 if rating is None else rating, *title)
    never_played = item.last_played_at is None
    reference = _moment(item.added_at if never_played else item.last_played_at)
    return (0, 0 if never_played else 1,
            0.0 if reference is None else reference, *title)


# Recent means the last 30 days, bounded to 50 rows; History retains all. The
# cancelled request keeps its own library join: a request whose item lost its
# library row is not shown here, exactly as before.
_CANCELLED = (
    ' JOIN libraries l ON l.id = mi.library_id'
    " WHERE rr.state='cancelled' AND julianday(rr.updated_at)>=julianday(?)"
)
_CANCELLED_ORDER = ' ORDER BY rr.updated_at DESC, rr.id DESC LIMIT 50'


def _recent_cancellations(conn, settings, clock, in_scope) -> list:
    """The cancelled requests of the last 30 days, inside the curated scope."""
    sql = _LEAVING_SELECT + _CANCELLED
    keys = library_scope.curated_library_keys(settings)
    if keys:
        sql += ' AND l.plex_key IN (%s)' % ', '.join('?' for _ in keys)
    window = (clock() - timedelta(days=30)).isoformat()
    return _leaving_rows(conn, sql + _CANCELLED_ORDER, (window, *keys), in_scope)


def _leaving_rows(conn, sql: str, params, in_scope):
    """The rows of one Leaving soon read that are inside the curated scope."""
    return [row for row in conn.execute(sql, params) if in_scope(row['media_item_id'])]


def _decoded_requests(engine, rows):
    """One ``RemovalRequest`` per row, ``None`` for a carrier that cannot read.

    S16 rule 1: each request is decoded on its own, so one damaged scope
    carrier is reported as unreadable instead of breaking the screen.
    """
    requests = []
    for row in rows:
        try:
            requests.append(engine._to_request(row))
        except Exception:
            logger.warning('web_request_unreadable', extra={'request_id': row['id']})
            requests.append(None)
    return requests


def _leaving_items(conn, rows, requests, clock, locale, episodes):
    """The rows of the screen, built without a query of their own (S1)."""
    return tuple(
        _unreadable(row) if request is None
        else _leaving_item(conn, request, clock, locale, item=row, episodes=episodes)
        for row, request in zip(rows, requests))


def leaving_soon_view(conn, settings, *, clock=now, facts=None, locale=None,
                      sort=None, cancelled=True) -> LeavingView:
    """The Leaving soon screen of one request.

    #148 S1: one joined statement reads every open request with its scope
    carrier and its item's display facts, and one grouped statement reads the
    episodes of every series on the screen; a row asks the database nothing
    of its own. ``cancelled`` is off for the screens that never show the
    recent cancellations, so no other tab pays for them.
    """
    engine = LifecycleEngine(conn, settings=settings, clock=clock)
    # The reader's language (a language switch or the setting), not the one
    # the request was marked in (#47).
    locale = locale or settings.locale
    facts = facts if facts is not None else _Page(conn, settings)
    # S14 rule 4: requests of deselected libraries stay in the database but
    # never reach the Leaving soon screen.
    allowed = facts.curated()
    in_scope = lambda media_item_id: allowed is None or media_item_id in allowed
    rows = _leaving_rows(
        conn, _LEAVING_SELECT + " WHERE rr.state NOT IN ('cancelled', 'deleted')"
        ' ORDER BY rr.due_at, rr.id', (), in_scope)
    recent = (_recent_cancellations(conn, settings, clock, in_scope)
              if cancelled else [])
    requests = _decoded_requests(engine, rows)
    recent_requests = _decoded_requests(engine, recent) if recent else []
    # One grouped read answers the size, the disks and the file count of every
    # series and seasons row of the screen: a movie carries its own file.
    series = {row['plex_rating_key']
              for row, request in [*zip(rows, requests), *zip(recent, recent_requests)]
              if request is not None and request.unit != Unit.MOVIE}
    episodes = _scope_episodes(conn, series)
    open_items = _leaving_items(conn, rows, requests, clock, locale, episodes)
    chosen = sort if sort in LEAVING_SORTS else LEAVING_DEFAULT_SORT
    if chosen != LEAVING_DEFAULT_SORT:
        # The current order (the soonest deletion date) is the SQL order the
        # rows already arrive in; any other one is sorted here, in Python.
        open_items = tuple(sorted(open_items,
                                  key=lambda item: _leaving_sort_key(item, chosen)))
    cancelled_items = _leaving_items(conn, recent, recent_requests,
                                     clock, locale, episodes)
    return LeavingView(open_items, cancelled_items, chosen)


def summary_view(conn, settings, *, clock=now, facts=None) -> SummaryView:
    facts = facts if facts is not None else _Page(conn, settings)
    scope = scan_rows.Scope(conn, settings)
    leaving = leaving_soon_view(conn, settings, clock=clock, facts=facts, cancelled=False)
    return _summary(conn, settings, scan_rows.totals(conn, settings=settings, scope=scope),
                    leaving, facts.run, scope=scope)


def go_live_view(conn, settings, *, clock=now) -> GoLiveView:
    """What turning dry run off starts: the items now due, and when (#64).

    Read before the setting is stored, so the confirmation the owner reads
    names exactly the removals that would run next.
    """
    leaving = leaving_soon_view(conn, settings, clock=clock, cancelled=False)
    first_due = next((item.due_label for item in leaving.items if item.due_label), None)
    return GoLiveView(len(leaving.items), sum(item.size_bytes for item in leaving.items),
                      first_due, kill_switch_enabled(conn))


def _summary(conn, settings, totals, leaving, run, *, scope=None):
    """The four summary cards, counted in SQL over the cached scan rows."""
    totals_of = lambda kind: Totals(totals.get(kind, (0, 0))[1], totals.get(kind, (0, 0))[0])
    deleted = conn.execute("SELECT COALESCE(SUM(freed_bytes),0), COUNT(*),"
                           " MIN(substr(occurred_at,1,7)) FROM removal_events WHERE outcome='deleted'").fetchone()
    return SummaryView(totals_of('candidate'), Totals(sum(row.size_bytes for row in leaving.items),
                                                      len(leaving.items)),
                       totals_of('protected'), settings.grace_days,
                       scan_rows.protection_kinds(conn, settings=settings, scope=scope),
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
    # #141: the poster of the event's item, read like the other cards read
    # it, from the item the sync stored. Only the History card carries it.
    values['media_item_id'] = row['media_item_id']
    item = None if row['media_item_id'] is None else conn.execute(
        'SELECT title, year, imdb_id, thumb, imdb_rating, rt_critic FROM media_items WHERE id = ?',
        (row['media_item_id'],)).fetchone()
    values['thumb'] = item['thumb'] if item is not None else None
    # #153: the IMDb address of the item, or of the title the event stored
    # when the item itself is gone (its year is then unknown).
    values['imdb_url'] = imdb_url(
        item['imdb_id'] if item is not None else None,
        (item['title'] if item is not None else None) or values['title'],
        item['year'] if item is not None else None)
    # #162: the scores of the item, and the Rotten Tomatoes search for its
    # title (or the event's stored title when the item is gone).
    values['imdb_rating'] = item['imdb_rating'] if item is not None else None
    values['rt_critic'] = item['rt_critic'] if item is not None else None
    values['rt_url'] = rt_url((item['title'] if item is not None else None) or values['title'])
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
    server-level facts, so their counts do not follow the scope.

    #148 S3: the Library page and the shell it renders used to build this
    panel twice, and the shell leaf of every screen asks for it; one read per
    request answers them all. Outside a request nothing is kept, so another
    worker's write is visible on the next read.
    """
    if has_request_context():
        if 'sources_view' in g:
            return g.sources_view
        view = _sources_view(conn, settings)
        g.sources_view = view
        return view
    return _sources_view(conn, settings)


def _sources_view(conn, settings) -> SourcesView:
    """The panel's own read: curated disks, viewers and the Plex history."""
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


# S2 (#144): one attempt, and at most five seconds, for every probe. All of
# them run at once, so the whole test answers in about ten seconds at worst.
CONNECTION_PROBE_TIMEOUT_SECONDS = 5.0


def _probe_options(timeout: float = CONNECTION_PROBE_TIMEOUT_SECONDS) -> dict:
    """The client options every probe shares: one short, bounded attempt (S2)."""
    return {"timeout": timeout, "max_retries": 0, "backoff_seconds": 0}


def _probes(settings):
    # Construct clients inside each probe: invalid configuration is isolated too.
    # S2 (#144): no retries and one short timeout, so a hanging service costs
    # the test one bounded call instead of a retry ladder of minutes.
    from almali_library_curator.plex import PlexClient
    from almali_library_curator.sonarr import SonarrClient
    from almali_library_curator.radarr import RadarrClient
    from almali_library_curator.tautulli import TautulliClient
    from almali_library_curator.tmdb import TmdbClient
    from almali_library_curator.hostfs import HostFilesystem

    def host():
        fs = HostFilesystem.from_settings(settings)
        return all(fs.is_mounted(path) for path in (settings.media_root, settings.unraid_root) if path)

    return {'plex': lambda: PlexClient.from_settings(
                settings, **_probe_options(
                    min(settings.plex_timeout_seconds, CONNECTION_PROBE_TIMEOUT_SECONDS))).sections(),
            'sonarr': lambda: SonarrClient.from_settings(settings, **_probe_options()).system_status(),
            'radarr': lambda: RadarrClient.from_settings(settings, **_probe_options()).system_status(),
            'tautulli': lambda: TautulliClient.from_settings(settings, **_probe_options()).users(),
            'tmdb': lambda: TmdbClient.from_settings(settings, **_probe_options()).movie(550),
            'host': host}


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
# S2 (#144): every probe at once, so the whole test answers in about ten
# seconds even when every service hangs. The pool is bounded by the cards
# themselves: a live test never outgrows the services it is testing.
CONNECTION_PROBE_WORKERS = len(CONNECTION_SERVICES)


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


def _probe_status(name, probe) -> str:
    """One card's status: 'unreachable' unless its probe answered (S7).

    The diagnostic stays in the log: an external error body never reaches a
    page, and one broken service never hides the others.
    """
    try:
        return 'unreachable' if probe() is False else 'connected'
    except Exception:
        logger.warning('web_connection_unreachable', extra={'service': name})
        return 'unreachable'


def _probe_all(probes, names) -> dict[str, str]:
    """Every configured probe at once, answered in the cards' own order (S2).

    A hanging service costs the test one bounded call instead of one per card
    in turn: with 1 worker x 4 request threads, the Connections test cannot
    spend minutes probing a broken install (#144).
    """
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(CONNECTION_PROBE_WORKERS, len(names)),
                            thread_name_prefix='connection-probe') as pool:
        submitted = {name: pool.submit(_probe_status, name, probes[name]) for name in names}
        return {name: submitted[name].result() for name in names}


def connections_view(conn, settings, *, probes=None, probe=True,
                     open_service=None) -> ConnectionsView:
    """Service status; without ``probe`` only the configuration is read.

    Probing calls every configured service, so a plain page load shows
    'configured' and the owner asks for a live test explicitly. The probes of
    one test run concurrently, each with one bounded attempt (S2, #144). The
    services come in the order the Connections cards read them (S7): Plex, the
    one required connection, first, then the optional ones and the host paths.
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
    tested = {}
    if probe:
        tested = _probe_all(probes, [name for name in CONNECTION_SERVICES if configured[name]])
    services = []
    for name in CONNECTION_SERVICES:
        available = configured[name]
        status = 'not_configured'
        if available and not probe:
            status = 'configured'
        elif available:
            status = tested[name]
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
                     'rating_source', 'protected_libraries', 'curated_libraries', 'excluded_users',
                     'scan_time', 'dry_run', 'radarr_deletes', 'sonarr_deletes')


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
                 locale=None, page=1, per_page=LIBRARY_PAGE_SIZE, sort=None) -> LibraryView:
    """The Library screen, with only the tab the owner asked for built (#48).

    The candidate and protected tables read one page of the cached scan rows
    each; the other tab is a count, never a second page. Leaving soon keeps
    its own lifecycle read, because its rows come from the requests. Each
    list reads the order the request asked for, or its own default (#134).
    One :class:`Scope` answers every reader of the page's rows (#148 S3), and
    the cancelled requests are built only for the tab that shows them (#148).
    """
    facts = _Page(conn, settings)
    scope = scan_rows.Scope(conn, settings)
    totals = scan_rows.totals(conn, settings=settings, scope=scope)
    candidates = protected = None
    if tab == 'candidates':
        candidates = _page_view(conn, 'candidate', category=category, page=page,
                                per_page=per_page, settings=settings, facts=facts, sort=sort,
                                scope=scope)
    if tab == 'protected':
        protected = _page_view(conn, 'protected', page=page, per_page=per_page,
                               settings=settings, facts=facts, sort=sort, scope=scope)
    candidates = candidates or _tab_view('candidate', totals, facts, per_page)
    protected = protected or _tab_view('protected', totals, facts, per_page)
    leaving = leaving_soon_view(conn, settings, clock=clock, facts=facts, locale=locale,
                                sort=sort, cancelled=tab == 'leaving_soon')
    return LibraryView(_summary(conn, settings, totals, leaving, facts.run, scope=scope),
                       candidates, protected, leaving,
                       sources_view(conn, settings), tab, redownload_warnings(settings),
                       dry_run=settings.dry_run, kill_switch=kill_switch_enabled(conn))


# -- duplicates (#132) --------------------------------------------------------
# Read-only. Two lists: the same title in more than one curated library,
# matched by the external id Plex already reports, and one item holding more
# than one version. Nothing here marks, deletes or adds to a freed-space
# figure; deleting a version is #133.

# The identity two copies are matched on, most specific first: the same
# movie in "Movies" and "Movies 4K" carries the same TMDB id. An item Plex
# gave no external id answers NULL here and is never grouped by its title.
_EXTERNAL_KEY = (
    "CASE WHEN mi.tmdb_id IS NOT NULL THEN 'tmdb:' || mi.tmdb_id"
    " WHEN mi.imdb_id IS NOT NULL THEN 'imdb:' || mi.imdb_id"
    " WHEN mi.tvdb_id IS NOT NULL THEN 'tvdb:' || mi.tvdb_id END"
)

_DUPLICATE_ITEM_COLUMNS = (
    "mi.id, mi.title, mi.item_type, mi.library_id, mi.file_size_bytes, mi.thumb,"
    " mi.year, mi.imdb_id, mi.imdb_rating, mi.rt_critic,"
    f" l.title AS library_title, {_EXTERNAL_KEY} AS match_key"
)

#: How many ids one lookup statement carries: a library of any size is read
#: in bounded statements instead of one huge ``IN`` list.
_LOOKUP_CHUNK = 400


def _duplicates_scope(conn, settings) -> tuple[str, tuple]:
    """The curated scope (S14 rule 4) as a predicate over ``media_items mi``.

    Proven-absent Plex rows stay outside it, as on every other screen. An
    empty selection curates every library, so the only condition is the
    presence Plex proved.
    """
    keys = library_scope.curated_library_keys(settings) if settings is not None else ()
    if keys:
        marks = ', '.join('?' for _ in keys)
        return f"mi.plex_present = 1 AND l.plex_key IN ({marks})", tuple(keys)
    return "mi.plex_present = 1", ()


def _duplicate_items(conn, where: str, params: tuple) -> list:
    """The items a duplicates query matched, with the facts a copy shows."""
    return conn.execute(
        f"SELECT {_DUPLICATE_ITEM_COLUMNS}"
        " FROM media_items mi LEFT JOIN libraries l ON l.id = mi.library_id"
        f" WHERE {where}",
        params,
    ).fetchall()


def _chunks(values) -> list[list[int]]:
    """The ids of one lookup, in the bounded chunks its statements carry."""
    ids = list(values)
    return [ids[start:start + _LOOKUP_CHUNK]
            for start in range(0, len(ids), _LOOKUP_CHUNK)]


def _versions_of(conn, item_ids) -> dict[int, list]:
    """The stored versions of the given items, in the order Plex sent them."""
    versions: dict[int, list] = {}
    for chunk in _chunks(item_ids):
        marks = ', '.join('?' for _ in chunk)
        for row in conn.execute(
            "SELECT media_item_id, resolution, video_codec, bitrate_kbps, size_bytes"
            f" FROM media_versions WHERE media_item_id IN ({marks})"
            " ORDER BY media_item_id, position",
            chunk,
        ):
            versions.setdefault(row["media_item_id"], []).append(row)
    return versions


def _kept_forever(conn, item_ids) -> set[int]:
    """The items the owner pressed "Keep forever" on, read by its own index."""
    kept: set[int] = set()
    for chunk in _chunks(item_ids):
        marks = ', '.join('?' for _ in chunk)
        kept.update(
            int(row[0]) for row in conn.execute(
                "SELECT media_item_id FROM protections"
                f" WHERE kind = 'keep_forever' AND media_item_id IN ({marks})",
                chunk,
            )
        )
    return kept


def _protected_library_ids(conn, settings) -> frozenset[int]:
    """The protected libraries, by the rule the protection engine reads (S4).

    ``libraries.protected`` or ``settings.protected_libraries`` naming the
    library's Plex key or its title. A protected copy stays in the view and
    says so: the owner asked to be told about it.
    """
    names = {
        name.strip()
        for name in (settings.protected_libraries if settings is not None else ())
        if name.strip()
    }
    titles = {name.casefold() for name in names}
    return frozenset(
        int(row["id"])
        for row in conn.execute("SELECT id, plex_key, title, protected FROM libraries")
        if row["protected"]
        or row["plex_key"] in names
        or (row["title"] or "").casefold() in titles
    )


def _item_copies(item, versions, protected_libraries, kept) -> tuple[DuplicateCopy, ...]:
    """Every copy of one item: one per version, or the item's own one file.

    A copy's markers say what keeps it safe, and they are on the copy, not on
    the group: two copies of one title can be kept for different reasons.
    """
    markers = tuple(
        marker for marker, applies in (
            ("protected_library", item["library_id"] in protected_libraries),
            ("keep_forever", item["id"] in kept),
        ) if applies
    )
    library = item["library_title"]
    facts = versions.get(item["id"]) or ()
    if not facts:
        # An item with no Media entry stores no version: its own first file
        # and size are the only facts it carries, and the rest is unknown.
        return (DuplicateCopy(item["id"], library, None, None,
                              item["file_size_bytes"], None, markers),)
    return tuple(
        DuplicateCopy(item["id"], library, row["resolution"], row["video_codec"],
                      row["size_bytes"], row["bitrate_kbps"], markers)
        for row in facts
    )


def _duplicate_group(title, item_type, copies, *, media_item_id=0, thumb=None,
                     imdb_url=None, imdb_rating=None, rt_critic=None,
                     rt_url=None) -> DuplicateGroup:
    copies = tuple(copies)
    return DuplicateGroup(title, item_type, copies,
                          sum(copy.size_bytes or 0 for copy in copies),
                          media_item_id, thumb, imdb_url,
                          imdb_rating=imdb_rating, rt_critic=rt_critic, rt_url=rt_url)


def _largest_first(groups) -> list[DuplicateGroup]:
    return sorted(groups, key=lambda group: (-group.size_bytes,
                                             group.title.casefold(), group.title))


def duplicates_view(conn, settings, *, limit: int = DUPLICATES_GROUP_LIMIT) -> DuplicatesView:
    """The Duplicates screen: the same title twice, and one item with versions.

    Both lists read only stored facts: the item rows the sync wrote and the
    versions of migration 016. The scope is the curated libraries, proven-
    absent rows excluded, exactly as the other screens read it. The view
    counts nothing toward freed space and offers no action.
    """
    scope, params = _duplicates_scope(conn, settings)
    # (a) The same title in more than one curated library, matched by the
    # external id. A season is a child of a series, never its own copy.
    shared = _duplicate_items(
        conn, f"{scope} AND mi.item_type IN ('movie', 'series') ORDER BY mi.id", params)
    by_identity: dict[tuple[str, str], list] = {}
    for row in shared:
        if row["match_key"]:
            by_identity.setdefault((row["item_type"], row["match_key"]), []).append(row)
    shared_groups = [rows for rows in by_identity.values()
                     if len({row["library_id"] for row in rows}) > 1]
    # (b) One item holding more than one version.
    multi_version = _duplicate_items(
        conn,
        f"{scope} AND (SELECT COUNT(*) FROM media_versions v"
        " WHERE v.media_item_id = mi.id) > 1"
        " ORDER BY (SELECT COALESCE(SUM(v.size_bytes), 0) FROM media_versions v"
        "           WHERE v.media_item_id = mi.id) DESC, mi.title, mi.id"
        f" LIMIT {int(limit) + 1}",
        params,
    )
    truncated = len(multi_version) > limit
    multi_version = multi_version[:limit]

    involved = [row["id"] for row in multi_version]
    for rows in shared_groups:
        involved.extend(row["id"] for row in rows)
    versions = _versions_of(conn, involved)
    kept = _kept_forever(conn, involved)
    protected = _protected_library_ids(conn, settings)

    same_title = _largest_first(
        _duplicate_group(rows[0]["title"], rows[0]["item_type"],
                         [copy for row in rows
                          for copy in _item_copies(row, versions, protected, kept)],
                         media_item_id=rows[0]["id"], thumb=rows[0]["thumb"],
                         imdb_url=imdb_url(rows[0]["imdb_id"], rows[0]["title"],
                                           rows[0]["year"]),
                         imdb_rating=rows[0]["imdb_rating"], rt_critic=rows[0]["rt_critic"],
                         rt_url=rt_url(rows[0]["title"]))
        for rows in shared_groups
    )
    truncated = truncated or len(same_title) > limit
    return DuplicatesView(
        tuple(same_title[:limit]),
        tuple(_duplicate_group(row["title"], row["item_type"],
                               _item_copies(row, versions, protected, kept),
                               media_item_id=row["id"], thumb=row["thumb"],
                               imdb_url=imdb_url(row["imdb_id"], row["title"],
                                                 row["year"]),
                               imdb_rating=row["imdb_rating"], rt_critic=row["rt_critic"],
                               rt_url=rt_url(row["title"]))
              for row in multi_version),
        limit,
        truncated,
    )
