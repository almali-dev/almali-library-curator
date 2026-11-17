"""Durable disk sets for the sources panel, written with the facts they describe.

The recorded sets exist so a render of the sources panel never has to scan
``media_items`` and ``episodes`` (#12). That only pays off if the record is
always usable, and it is only safe if the record always describes the
*committed* database (#36). Both writers keep that rule, and neither
invalidates the record first:

- the file refresh records it in the transaction that rewrites the disk
  facts (``enrichment.files``);
- the scan records it in the transaction that publishes the sync, the only
  other writer that can change an item's library (``scan.pipeline``).

So a failed sync or refresh commits neither the facts nor the record: the
previous record still describes the database and stays usable, and the panel
never has to fall back to the live query — which is the slow path, and the
one a broken service could otherwise hold forever.
"""
from __future__ import annotations

import json
import sqlite3

_CACHE_KEY = 'sources_disks_v1'


def record_disks(conn: sqlite3.Connection) -> None:
    """Summarize stored facts once per write, not once per web request.

    Keep every library, including deselected libraries' untouched facts, so
    changing the curated selection needs no new scan. The global set also
    includes orphans; scoped episodes follow the panel's series-key join,
    deliberately not the file refresher's media_item_id join.

    The caller owns the transaction: the cache has to commit or roll back
    with the facts it describes, or the panel would be fast and wrong. An
    empty result is a valid answer and is recorded as one. app_meta already
    exists on both fresh and upgraded installs.
    """
    disks: set[str] = set()
    libraries: dict[str, set[str]] = {}
    for key, disk in conn.execute(
        'SELECT l.plex_key, mi.disk FROM media_items mi'
        ' LEFT JOIN libraries l ON l.id=mi.library_id'
        ' UNION SELECT l.plex_key, e.disk FROM episodes e'
        ' LEFT JOIN media_items s ON s.plex_rating_key=e.series_rating_key'
        ' LEFT JOIN libraries l ON l.id=s.library_id'
    ):
        if not disk:
            continue
        disks.add(disk)
        if key is not None:
            libraries.setdefault(key, set()).add(disk)
    value = {'all': sorted(disks),
             'libraries': {key: sorted(values) for key, values in libraries.items()}}
    conn.execute('INSERT INTO app_meta (key, value) VALUES (?, ?)'
                 ' ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (_CACHE_KEY, json.dumps(value, separators=(',', ':'))))


def cached_disks(conn: sqlite3.Connection, keys: tuple[str, ...]) -> set[str] | None:
    """Read shared SQLite metadata; None means use the pre-cache query.

    An empty set is a valid cached answer. Unreadable/older metadata is not:
    the panel remains usable even before its first successful write, and a
    value an older version invalidated (``'null'``) is read as missing until
    the next sync or refresh records a fresh one.
    """
    row = conn.execute('SELECT value FROM app_meta WHERE key=?', (_CACHE_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
        if not isinstance(value, dict):
            return None
        disks, libraries = value['all'], value['libraries']
        if not _disk_list(disks) or not isinstance(libraries, dict) or not all(
                _disk_list(values) for values in libraries.values()):
            return None
        return ({disk for key in keys for disk in libraries.get(key, []) if disk}
                if keys else {disk for disk in disks if disk})
    except (KeyError, TypeError, ValueError):
        return None


def _disk_list(values) -> bool:
    return isinstance(values, list) and all(isinstance(disk, str) for disk in values)
