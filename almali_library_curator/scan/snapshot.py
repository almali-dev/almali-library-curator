"""Credential-free display facts captured while the scan's database exists."""
from almali_library_curator.detection.models import Unit


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
    item = conn.execute('SELECT rating, rating_source, last_played_at FROM media_items WHERE id = ?',
                        (candidate.media_item_id,)).fetchone()
    _, disk = scope_storage(conn, candidate.media_item_id, candidate.unit, candidate.seasons)
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
        'size_bytes': candidate.size_bytes, 'disk': disk,
        'rating': candidate.rating, 'rating_source': source,
        # #48: the last activity the row was measured with, kept beside the
        # other display facts so the Library's cache carries it too.
        'last_played_at': item['last_played_at'] if item else None,
    }


def detection_snapshot(conn, outcome):
    return {
        'candidates': len(outcome.candidates), 'protected': len(outcome.protected),
        'candidate_rows': [candidate_snapshot(conn, row) for row in outcome.candidates],
        'protected_rows': [dict(candidate_snapshot(conn, row.candidate),
                                protection_kind=row.protection.kind.value,
                                protection_reason=row.reason,
                                protection_reason_parts=[dict(part) for part in row.reason_parts]) for row in outcome.protected],
    }
