"""Durable disk sets for the sources panel, atomic with refreshed file facts."""
from __future__ import annotations

import json
import sqlite3

_CACHE_KEY = 'sources_disks_v1'


def record_disks(conn: sqlite3.Connection) -> None:
    """Summarize stored facts once per refresh, not once per web request.

    Keep every library, including deselected libraries' untouched facts, so
    changing the curated selection needs no new scan. The global set also
    includes orphans; scoped episodes follow the panel's series-key join,
    deliberately not the file refresher's media_item_id join.

    The caller owns the transaction: the cache and disk facts must commit
    together. app_meta already exists on both fresh and upgraded installs.
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


def invalidate_disks(conn: sqlite3.Connection) -> None:
    """Stop serving an old library mapping before sync can change it.

    A failed sync or refresh must leave the panel on its live-query fallback,
    not a stale scope. In a dry run this affects only the private preview.
    The caller owns the transaction.
    """
    conn.execute('UPDATE app_meta SET value=? WHERE key=?', ('null', _CACHE_KEY))


def cached_disks(conn: sqlite3.Connection, keys: tuple[str, ...]) -> set[str] | None:
    """Read shared SQLite metadata; None means use the pre-cache query.

    An empty set is a valid cached answer. Unreadable/older metadata is not:
    the panel remains usable even before its first successful refresh.
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
