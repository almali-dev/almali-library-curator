"""The published scan snapshot as paged, filtered rows (#48).

Migration 014 keeps the latest published snapshot as one typed row per
candidate and per protected item, and its triggers rebuild those rows
whenever that snapshot is written. This module reads them: the Library page
asks for one page, one count and one set of category counts, and SQLite does
the ordering, the filtering and the counting. Nothing here re-reads or
re-parses the snapshot JSON.
"""

from __future__ import annotations

import json

from almali_library_curator.detection.models import Category, Unit
from almali_library_curator import library_scope

#: The kinds of row the cache holds, one table column each.
KINDS = ('candidate', 'protected')

#: The columns the Library page displays, in the order ``ItemView`` reads them.
COLUMNS = (
    'media_item_id', 'unit', 'seasons', 'title', 'type_badge', 'categories',
    'reasons', 'reason', 'reason_parts', 'size_bytes', 'disk', 'last_played_at',
    'rating', 'rating_source', 'protection_kind', 'protection_reason',
    'protection_reason_parts',
    # #139: the poster of the row's item, copied from the published snapshot
    # when the row was written. NULL means no poster, never a broken image.
    'thumb',
    # #153: the IMDb link of the row's item, built by the scan and copied with
    # the row. NULL means no link.
    'imdb_url',
)

#: The orders the list can be asked for (#134): size (largest first), age
#: (added longest ago), rating (lowest first) and time without plays (longest
#: first). The current order stays the default.
SORTS = ('size', 'age', 'rating', 'unplayed')
DEFAULT_SORT = 'size'

#: The SQL order of every sort key (#134), starting with the published order:
#: largest first, then by title and item id, so the pages of two requests
#: agree and page 2 continues page 1. ``media_items`` joins the rows only for
#: the two orders that read a date, because the cache keeps no added date of
#: its own. Every order ends with the title and the item id.
_SQL_ORDERS = {
    'size': 'r.size_bytes DESC, r.title, r.media_item_id',
    # A row with no added date sorts last.
    'age': 'media.added_at IS NULL, media.added_at, r.title, r.media_item_id',
    # A row with no rating sorts last.
    'rating': 'r.rating IS NULL, r.rating, r.title, r.media_item_id',
    # The detection engine's reference: the last play, else the added date.
    # A never-played row comes first, and an unknown one before every dated
    # row, so a missing instant never reads as long ago.
    'unplayed': ('(r.last_played_at IS NULL) DESC,'
                 ' COALESCE(r.last_played_at, media.added_at),'
                 ' r.title, r.media_item_id'),
}

#: The columns of an order that read ``media_items``, joined by that order.
_JOINED_SORTS = frozenset({'age', 'unplayed'})

#: The placeholder :meth:`Scope.predicate` fills with the table prefix of the
#: query it is spliced into.
_PREFIX = '{prefix}'


class Scope:
    """The curated scope of one request, resolved once (#148 S3).

    Every reader of the Library's rows asks the same question: which stored
    item ids are inside the curated libraries, with every row Plex proved
    absent left out. Asking it cost one probe and one subquery per reader,
    six to eight times per page; one :class:`Scope` answers all of them, and
    the page builds exactly one.
    """

    def __init__(self, conn, settings) -> None:
        self._conn = conn
        self._settings = settings
        self._resolved: tuple[str, tuple] | None = None

    def predicate(self, prefix: str = '') -> tuple[str, tuple]:
        """The scope as a SQL predicate, its column qualified by ``prefix``."""
        if self._resolved is None:
            self._resolved = _scope_template(self._conn, self._settings)
        sql, params = self._resolved
        return sql.replace(_PREFIX, prefix), params


def active_sort(sort: str | None) -> str:
    """The sort a request asked for, or the default when it is unknown (#134)."""
    return sort if sort in _SQL_ORDERS else DEFAULT_SORT


def _scope_template(conn, settings) -> tuple[str, tuple]:
    """The curated scope as a predicate template, built once (#148 S3).

    ``None`` settings means no restriction, exactly as in the other screen
    builders, and so does a store with no proven-absent row: then every
    stored id is in scope, including the ids of a dry run that never wrote a
    media item. The column is left as :data:`_PREFIX` so one resolution
    serves every query that splices it in.
    """
    if settings is None:
        return '1 = 1', ()
    keys = library_scope.curated_library_keys(settings)
    if not keys and not library_scope.any_proven_absent(conn):
        return '1 = 1', ()
    predicate = (
        f'{_PREFIX}media_item_id IN (SELECT mi.id FROM media_items mi'
        ' JOIN libraries l ON l.id = mi.library_id WHERE mi.plex_present = 1'
    )
    if not keys:
        return predicate + ')', ()
    marks = ', '.join('?' for _ in keys)
    return predicate + f' AND l.plex_key IN ({marks}))', tuple(keys)


def _scope(conn, settings, prefix: str = '') -> tuple[str, tuple]:
    """The curated scope as a SQL predicate (S14 rule 4).

    Proven-absent Plex items and deselected libraries stay stored but are
    outside the scope. A page resolves it once through :class:`Scope` and
    hands that answer to every reader; this one-shot form stays for the
    callers that ask a single question.
    """
    sql, params = _scope_template(conn, settings)
    return sql.replace(_PREFIX, prefix), params


def _decoded(raw):
    """One stored JSON list, or an empty tuple when it cannot be read."""
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    return tuple(value) if isinstance(value, list) else ()


def decoded_row(row) -> dict:
    """One cache row with its JSON columns decoded for the view models."""
    values = {name: row[name] for name in COLUMNS}
    for name in ('seasons', 'categories', 'reasons'):
        values[name] = _decoded(values[name])
    for name in ('reason_parts', 'protection_reason_parts'):
        values[name] = tuple(part for part in _decoded(values[name])
                             if isinstance(part, dict) and part.get('key'))
    return values


def _category_filter(category, kind: str) -> tuple[str, tuple]:
    """The category filter as a SQL predicate, served by its own index."""
    if not category or category == 'all':
        return '1 = 1', ()
    return ('r.id IN (SELECT scan_row_id FROM scan_row_categories'
            ' WHERE category = ? AND kind = ?)', (category, kind))


def page_rows(conn, kind: str, *, category=None, limit: int = 50, offset: int = 0,
              settings=None, sort: str | None = None, scope: Scope | None = None) -> list[dict]:
    """One page of the rows of ``kind``, filtered and ordered in SQL."""
    scope = scope if scope is not None else Scope(conn, settings)
    predicate, params = scope.predicate(prefix='r.')
    filtered, filter_params = _category_filter(category, kind)
    chosen = active_sort(sort)
    order = _SQL_ORDERS[chosen]
    join = (' LEFT JOIN media_items media ON media.id = r.media_item_id'
            if chosen in _JOINED_SORTS else '')
    columns = ', '.join(f'r.{name}' for name in COLUMNS)
    sql = (f'SELECT {columns} FROM scan_rows r{join}'
           f' WHERE r.kind = ? AND {predicate} AND {filtered}'
           f' ORDER BY {order} LIMIT ? OFFSET ?')
    values = [kind, *params, *filter_params, limit, offset]
    rows = [decoded_row(row) for row in conn.execute(sql, values)]
    _scope_counts(conn, rows)
    return rows


def _scope_counts(conn, rows) -> None:
    """Add what each series or seasons row would delete (#128).

    The counts are read from the synced episode rows of this page's items,
    never stored in the cache: migration 014's columns are fixed, and a
    derived value does not need a schema change. Proven-absent episodes are
    outside the scope, exactly as the size is.
    """
    wanted = sorted({row['media_item_id'] for row in rows
                     if row['unit'] in (Unit.SERIES.value, Unit.SEASONS.value)})
    if not wanted:
        return
    marks = ', '.join('?' for _ in wanted)
    series_keys = {int(row['id']): row['plex_rating_key'] for row in conn.execute(
        f'SELECT id, plex_rating_key FROM media_items WHERE id IN ({marks})', wanted)}
    if not series_keys:
        return
    key_marks = ', '.join('?' for _ in series_keys)
    episodes: dict[str, dict[int, int]] = {}
    for row in conn.execute(
            'SELECT series_rating_key, season_number, COUNT(*) FROM episodes'
            f' WHERE plex_present = 1 AND series_rating_key IN ({key_marks})'
            ' GROUP BY series_rating_key, season_number', tuple(series_keys.values())):
        episodes.setdefault(row[0], {})[row[1]] = int(row[2])
    for row in rows:
        key = series_keys.get(row['media_item_id'])
        if key is None:
            continue
        seasons = episodes.get(key, {})
        if row['unit'] == Unit.SEASONS.value:
            wanted_seasons = set(row['seasons'])
            row['episode_count'] = sum(count for number, count in seasons.items()
                                       if number in wanted_seasons)
        else:
            row['season_count'] = len(seasons)
            row['episode_count'] = sum(seasons.values())


def count_rows(conn, kind: str, *, category=None, settings=None,
               scope: Scope | None = None) -> tuple[int, int]:
    """How many rows of ``kind`` match, and how many bytes they take."""
    scope = scope if scope is not None else Scope(conn, settings)
    predicate, params = scope.predicate(prefix='r.')
    filtered, filter_params = _category_filter(category, kind)
    row = conn.execute(
        'SELECT COUNT(*), COALESCE(SUM(r.size_bytes), 0) FROM scan_rows r'
        f' WHERE r.kind = ? AND {predicate} AND {filtered}',
        [kind, *params, *filter_params]).fetchone()
    return int(row[0]), int(row[1])


def category_counts(conn, kind: str, *, settings=None,
                    scope: Scope | None = None) -> dict[str, int]:
    """The counts of the category filters, of one kind, counted in SQL."""
    scope = scope if scope is not None else Scope(conn, settings)
    predicate, params = scope.predicate(prefix='r.')
    found = {row[0]: int(row[1]) for row in conn.execute(
        'SELECT c.category, COUNT(*) FROM scan_row_categories c'
        ' JOIN scan_rows r ON r.id = c.scan_row_id'
        f' WHERE c.kind = ? AND {predicate} GROUP BY c.category', [kind, *params])}
    total = count_rows(conn, kind, settings=settings, scope=scope)[0]
    return {'all': total, **{category.value: found.get(category.value, 0)
                             for category in Category}}


def totals(conn, *, settings=None,
           scope: Scope | None = None) -> dict[str, tuple[int, int]]:
    """Count and bytes of every kind in scope, in one query."""
    scope = scope if scope is not None else Scope(conn, settings)
    predicate, params = scope.predicate()
    return {row[0]: (int(row[1]), int(row[2])) for row in conn.execute(
        'SELECT kind, COUNT(*), COALESCE(SUM(size_bytes), 0) FROM scan_rows'
        f' WHERE {predicate} GROUP BY kind', params)}


def protection_kinds(conn, *, settings=None,
                     scope: Scope | None = None) -> tuple[str, ...]:
    """The protection sources shown by the summary card, in SQL."""
    scope = scope if scope is not None else Scope(conn, settings)
    predicate, params = scope.predicate()
    return tuple(row[0] for row in conn.execute(
        "SELECT DISTINCT protection_kind FROM scan_rows WHERE kind = 'protected'"
        f" AND protection_kind IS NOT NULL AND {predicate}"
        ' ORDER BY protection_kind', params))
