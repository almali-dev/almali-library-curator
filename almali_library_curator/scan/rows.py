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

from almali_library_curator.detection.models import Category
from almali_library_curator import library_scope

#: The kinds of row the cache holds, one table column each.
KINDS = ('candidate', 'protected')

#: The columns the Library page displays, in the order ``ItemView`` reads them.
COLUMNS = (
    'media_item_id', 'unit', 'seasons', 'title', 'type_badge', 'categories',
    'reasons', 'reason', 'reason_parts', 'size_bytes', 'disk', 'last_played_at',
    'rating', 'rating_source', 'protection_kind', 'protection_reason',
    'protection_reason_parts',
)

#: The published order: largest first, then by title and item id, so the
#: pages of two requests agree and page 2 continues page 1.
ORDER_BY = 'size_bytes DESC, title, media_item_id'


def _scope(conn, settings, prefix: str = '') -> tuple[str, tuple]:
    """The curated scope as a SQL predicate (S14 rule 4).

    Proven-absent Plex items and deselected libraries stay stored but are
    outside the scope. ``None`` settings means no restriction, exactly as
    in the other screen builders, and so does a store with no proven-absent
    row: then every stored id is in scope, including the ids of a dry run
    that never wrote a media item.
    """
    if settings is None:
        return '1 = 1', ()
    keys = library_scope.curated_library_keys(settings)
    if not keys and conn.execute(
            'SELECT 1 FROM media_items WHERE plex_present = 0 LIMIT 1').fetchone() is None:
        return '1 = 1', ()
    predicate = (
        f'{prefix}media_item_id IN (SELECT mi.id FROM media_items mi'
        ' JOIN libraries l ON l.id = mi.library_id WHERE mi.plex_present = 1'
    )
    if not keys:
        return predicate + ')', ()
    marks = ', '.join('?' for _ in keys)
    return predicate + f' AND l.plex_key IN ({marks}))', tuple(keys)


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
    return ('id IN (SELECT scan_row_id FROM scan_row_categories'
            ' WHERE category = ? AND kind = ?)', (category, kind))


def page_rows(conn, kind: str, *, category=None, limit: int = 50, offset: int = 0,
              settings=None) -> list[dict]:
    """One page of the rows of ``kind``, filtered and ordered in SQL."""
    scope, params = _scope(conn, settings)
    filtered, filter_params = _category_filter(category, kind)
    sql = (f'SELECT {", ".join(COLUMNS)} FROM scan_rows'
           f' WHERE kind = ? AND {scope} AND {filtered}'
           f' ORDER BY {ORDER_BY} LIMIT ? OFFSET ?')
    values = [kind, *params, *filter_params, limit, offset]
    return [decoded_row(row) for row in conn.execute(sql, values)]


def count_rows(conn, kind: str, *, category=None, settings=None) -> tuple[int, int]:
    """How many rows of ``kind`` match, and how many bytes they take."""
    scope, params = _scope(conn, settings)
    filtered, filter_params = _category_filter(category, kind)
    row = conn.execute(
        'SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM scan_rows'
        f' WHERE kind = ? AND {scope} AND {filtered}',
        [kind, *params, *filter_params]).fetchone()
    return int(row[0]), int(row[1])


def category_counts(conn, kind: str, *, settings=None) -> dict[str, int]:
    """The counts of the category filters, of one kind, counted in SQL."""
    scope, params = _scope(conn, settings, prefix='r.')
    found = {row[0]: int(row[1]) for row in conn.execute(
        'SELECT c.category, COUNT(*) FROM scan_row_categories c'
        ' JOIN scan_rows r ON r.id = c.scan_row_id'
        f' WHERE c.kind = ? AND {scope} GROUP BY c.category', [kind, *params])}
    total = count_rows(conn, kind, settings=settings)[0]
    return {'all': total, **{category.value: found.get(category.value, 0)
                             for category in Category}}


def totals(conn, *, settings=None) -> dict[str, tuple[int, int]]:
    """Count and bytes of every kind in scope, in one query."""
    scope, params = _scope(conn, settings)
    return {row[0]: (int(row[1]), int(row[2])) for row in conn.execute(
        'SELECT kind, COUNT(*), COALESCE(SUM(size_bytes), 0) FROM scan_rows'
        f' WHERE {scope} GROUP BY kind', params)}


def protection_kinds(conn, *, settings=None) -> tuple[str, ...]:
    """The protection sources shown by the summary card, in SQL."""
    scope, params = _scope(conn, settings)
    return tuple(row[0] for row in conn.execute(
        "SELECT DISTINCT protection_kind FROM scan_rows WHERE kind = 'protected'"
        f" AND protection_kind IS NOT NULL AND {scope}"
        ' ORDER BY protection_kind', params))
