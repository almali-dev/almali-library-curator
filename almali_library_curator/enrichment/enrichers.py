"""Concrete detection protocols using linked Sonarr facts and TMDB fallback."""
from __future__ import annotations

import logging
import sqlite3

from almali_library_curator.detection.enrichers import Enrichers, SeriesStatus, SeriesStatusInfo
from almali_library_curator.tmdb import TmdbClient

logger = logging.getLogger(__name__)

_STATUS = {
    "continuing": SeriesStatus.CONTINUING,
    "returning series": SeriesStatus.CONTINUING,
    "in production": SeriesStatus.CONTINUING,
    "planned": SeriesStatus.CONTINUING,
    "ended": SeriesStatus.ENDED,
    "canceled": SeriesStatus.CANCELLED,
    "cancelled": SeriesStatus.CANCELLED,
}


def _rating(details) -> float | None:
    if details is None or details.vote_count == 0:
        return None
    value = details.vote_average
    return value if 0 <= value <= 10 else None


class TmdbEnricher:
    """Resolve by synced external id, never a potentially ambiguous title search.

    One instance is scan-scoped: cached answers and failures avoid repeated
    network reads within detection. Seasons inherit their series' identity.
    Sonarr's known status has precedence; TMDB supplies a missing status and
    ratings, not an override of a known Plex rating (the detection engine
    enforces that final precedence).
    """

    def __init__(self, conn: sqlite3.Connection, tmdb: TmdbClient | None = None) -> None:
        self.conn = conn
        self.tmdb = tmdb
        self._cache: dict[tuple[str, int], object] = {}

    def _item(self, rating_key):
        row = self.conn.execute("SELECT * FROM media_items WHERE plex_rating_key=?", (rating_key,)).fetchone()
        if row is not None and row["item_type"] == "season":
            row = self.conn.execute("SELECT * FROM media_items WHERE plex_rating_key=?",
                                    (row["parent_rating_key"],)).fetchone()
        return row

    def _details(self, row):
        if row is None or not row["tmdb_id"] or self.tmdb is None:
            return None
        key = (row["item_type"], row["tmdb_id"])
        if key not in self._cache:
            try:
                self._cache[key] = (self.tmdb.movie(key[1]) if key[0] == "movie"
                                    else self.tmdb.tv(key[1]))
            except Exception as error:
                self._cache[key] = None
                logger.warning("enrichment_tmdb_failed", extra={"error_type": type(error).__name__})
        return self._cache[key]

    def series_status(self, title: str, plex_rating_key: str) -> SeriesStatusInfo | None:
        row = self._item(plex_rating_key)
        if row is None or row["item_type"] != "series":
            return None
        status = _STATUS.get((row["production_status"] or "").casefold(), SeriesStatus.UNKNOWN)
        details = self._details(row)
        if details is None:
            return SeriesStatusInfo(status=status) if status is not SeriesStatus.UNKNOWN else None
        if status is SeriesStatus.UNKNOWN:
            status = _STATUS.get(details.status.casefold(), SeriesStatus.UNKNOWN)
        year = None
        if status in (SeriesStatus.ENDED, SeriesStatus.CANCELLED) and details.last_air_date:
            try:
                year = int(details.last_air_date[:4])
            except ValueError:
                pass
        return SeriesStatusInfo(status=status, ended_year=year, rating=_rating(details))

    def rating(self, title: str, item_type: str, plex_rating_key: str) -> float | None:
        return _rating(self._details(self._item(plex_rating_key)))


def build_enrichers(conn: sqlite3.Connection, *, tmdb: TmdbClient | None = None) -> Enrichers:
    """Build both existing detection protocols over a single scan-scoped cache."""
    provider = TmdbEnricher(conn, tmdb)
    return Enrichers(series_status=provider, rating=provider)
