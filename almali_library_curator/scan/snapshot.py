"""Credential-free display facts captured while the scan's database exists."""
import json

from almali_library_curator.detection.models import Unit
from almali_library_curator.imdb import imdb_url
from almali_library_curator.rottentomatoes import rt_url

#: #148 S5: how many ids one lookup statement carries, so a scan of any size
#: is read in bounded statements instead of one huge ``IN`` list.
SNAPSHOT_CHUNK = 400


def scope_storage(conn, media_item_id, unit, seasons=()):
    """Size and disks of the present scope, excluding proven-absent Plex rows."""
    item = conn.execute('SELECT * FROM media_items WHERE id = ?', (media_item_id,)).fetchone()
    if item is None or not item['plex_present']:
        return 0, None
    if unit == Unit.MOVIE:
        return item['file_size_bytes'] or 0, item['disk']
    files = conn.execute('SELECT file_size_bytes, disk, season_number FROM episodes'
                         ' WHERE series_rating_key = ? AND plex_present = 1', (item['plex_rating_key'],))
    rows = [row for row in files if unit != Unit.SEASONS or row['season_number'] in seasons]
    disks = sorted({row['disk'] for row in rows if row['disk']})
    return sum(row['file_size_bytes'] or 0 for row in rows), ', '.join(disks) or None


def _rating_source(item, rating):
    """Where the shown rating came from, or nothing when there is none.

    ``rating_source`` names the number stored with the item: Plex's critic
    rating, the Plex audience rating when the setting chose it, or a stored
    value an earlier version wrote. With no stored number, or when the shown
    number is higher than the stored one (the ``highest`` setting let TMDB
    win), the rating came from the TMDB fallback.
    """
    if rating is None:
        return None
    if item is None or item['rating'] is None or rating > item['rating']:
        return 'TMDB'
    stored = item['rating_source']
    if not stored:
        return 'Plex'
    return {'plex': 'Plex', 'plex_audience': 'Plex audience'}.get(stored, stored)


def candidate_snapshot(conn, candidate):
    """One candidate's display facts, read like a whole batch of them."""
    return candidate_snapshots(conn, (candidate,))[0]


def candidate_snapshots(conn, candidates):
    """Every candidate's display facts, read in two statements (#148 S5).

    The snapshot used to run about three statements per candidate; the item
    facts of the whole batch are one lookup and the disks of its series are
    one grouped read, so the cost no longer follows the number of rows.
    """
    candidates = list(candidates)
    items = _items_of(conn, {candidate.media_item_id for candidate in candidates})
    disks = _series_disks(conn, {
        items[candidate.media_item_id]['plex_rating_key']
        for candidate in candidates
        if candidate.unit != Unit.MOVIE
        and candidate.media_item_id in items
        and items[candidate.media_item_id]['plex_present']})
    return [_candidate_row(candidate, items.get(candidate.media_item_id), disks)
            for candidate in candidates]


def _items_of(conn, ids) -> dict:
    """The stored display columns of those items, by id."""
    found = {}
    wanted = sorted(ids)
    for start in range(0, len(wanted), SNAPSHOT_CHUNK):
        chunk = wanted[start:start + SNAPSHOT_CHUNK]
        marks = ', '.join('?' for _ in chunk)
        for row in conn.execute(
                'SELECT id, title, year, imdb_id, rating, rating_source, last_played_at,'
                ' thumb, plex_rating_key, plex_present, disk, imdb_rating, rt_critic'
                f' FROM media_items WHERE id IN ({marks})', chunk):
            found[int(row['id'])] = row
    return found


def _series_disks(conn, keys) -> dict:
    """The disks of every present episode, grouped by series and season."""
    found: dict = {}
    wanted = sorted(key for key in keys if key)
    for start in range(0, len(wanted), SNAPSHOT_CHUNK):
        chunk = wanted[start:start + SNAPSHOT_CHUNK]
        marks = ', '.join('?' for _ in chunk)
        for row in conn.execute(
                'SELECT series_rating_key, season_number,'
                ' json_group_array(DISTINCT disk) AS disks FROM episodes'
                f' WHERE plex_present = 1 AND series_rating_key IN ({marks})'
                ' GROUP BY series_rating_key, season_number', chunk):
            found.setdefault(row['series_rating_key'], {})[row['season_number']] = \
                disk_labels(row['disks'])
    return found


def disk_labels(raw) -> frozenset:
    """The disk labels of one grouped ``json_group_array(DISTINCT disk)`` read.

    Read defensively: a value this build cannot parse names no disk, and a
    missing disk stays missing instead of becoming an empty label.
    """
    try:
        labels = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(label for label in labels if isinstance(label, str) and label)


def _scope_disk(item, unit, seasons, disks):
    """The disk label of one scope, exactly as :func:`scope_storage` reports it."""
    if item is None or not item['plex_present']:
        return None
    if unit == Unit.MOVIE:
        return item['disk']
    per_season = disks.get(item['plex_rating_key'], {})
    labels = sorted({label for number, found in per_season.items()
                     if unit != Unit.SEASONS or number in seasons
                     for label in found})
    return ', '.join(labels) or None


def _candidate_row(candidate, item, disks):
    """The display row of one candidate, built from the batch's own facts."""
    source = _rating_source(item, candidate.rating)
    return {
        'media_item_id': candidate.media_item_id, 'unit': candidate.unit.value,
        'seasons': list(candidate.seasons), 'title': candidate.title,
        'type_badge': candidate.unit.value.upper(),
        'categories': [category.value for category in candidate.categories],
        'reasons': list(candidate.reasons), 'reason': '; '.join(candidate.reasons),
        # #47: the same reasons as keys plus raw parameters, so each request
        # renders them in its own language; the text above stays as fallback.
        'reason_parts': [dict(part) for part in candidate.reason_parts],
        'size_bytes': candidate.size_bytes,
        'disk': _scope_disk(item, candidate.unit, candidate.seasons, disks),
        'rating': candidate.rating, 'rating_source': source,
        # #48: the last activity the row was measured with, kept beside the
        # other display facts so the Library's cache carries it too.
        'last_played_at': item['last_played_at'] if item else None,
        # #139: the poster the sync stored, so a dry run (whose media_items
        # live only in the preview) reaches the screen through scan_rows. A
        # seasons row's media_item_id is its series, so it carries the series
        # poster.
        'thumb': item['thumb'] if item else None,
        # #153: the IMDb link of the item, built here from the item's own
        # title and year, so a seasons row (whose title is "1923 S2") searches
        # for the series. A dry run reaches the screen through scan_rows,
        # exactly as its poster does.
        'imdb_url': (imdb_url(item['imdb_id'], item['title'], item['year'])
                     if item else None),
        # #162: the scores Plex sent for the item (a seasons row carries its
        # series'), and the Rotten Tomatoes search for the item's own title.
        'imdb_rating': item['imdb_rating'] if item else None,
        'rt_critic': item['rt_critic'] if item else None,
        'rt_url': rt_url(item['title']) if item else None,
    }


def detection_snapshot(conn, outcome):
    """The display sections of one detection outcome, in two statements."""
    protected_candidates = [row.candidate for row in outcome.protected]
    rows = candidate_snapshots(conn, [*outcome.candidates, *protected_candidates])
    head = len(outcome.candidates)
    return {
        'candidates': len(outcome.candidates), 'protected': len(outcome.protected),
        'candidate_rows': rows[:head],
        'protected_rows': [
            dict(row, protection_kind=protected.protection.kind.value,
                 protection_reason=protected.reason,
                 protection_reason_parts=[dict(part) for part in protected.reason_parts])
            for row, protected in zip(rows[head:], outcome.protected)],
    }
