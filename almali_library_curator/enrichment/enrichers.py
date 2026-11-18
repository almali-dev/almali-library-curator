"""Concrete detection protocols using linked Sonarr facts and TMDB fallback."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from almali_library_curator.detection.enrichers import Enrichers, SeriesStatus, SeriesStatusInfo
from almali_library_curator.tmdb import TmdbClient, TmdbMovie, TmdbShow

logger = logging.getLogger(__name__)

# How long one TMDB answer is reused before it is asked again (#74). A
# production status or a vote average moves over months, never within one.
TMDB_CACHE_TTL_DAYS = 30

_STATUS = {
    "continuing": SeriesStatus.CONTINUING,
    "returning series": SeriesStatus.CONTINUING,
    "in production": SeriesStatus.CONTINUING,
    "planned": SeriesStatus.CONTINUING,
    "ended": SeriesStatus.ENDED,
    "canceled": SeriesStatus.CANCELLED,
    "cancelled": SeriesStatus.CANCELLED,
}

# The statuses whose reason sentence carries the year TMDB knows and the
# stored facts do not.
_YEAR_STATUSES = (SeriesStatus.ENDED, SeriesStatus.CANCELLED)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _rating(details) -> float | None:
    if details is None or details.vote_count == 0:
        return None
    value = details.vote_average
    return value if 0 <= value <= 10 else None


class TmdbCache:
    """The TMDB answers a later scan can reuse, with a TTL (#74).

    A cached answer is external fact, never scan state: it is what TMDB
    said, so a dry run may keep it (see ``scan.runner``) and a read-only
    database simply answers nothing. A payload older than the TTL is treated
    as absent, and a failed request is never stored, so a bad answer is
    asked for again on the next scan instead of being believed for a month.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        ttl_days: float = TMDB_CACHE_TTL_DAYS,
        now=_utcnow,
    ) -> None:
        self.conn = conn
        self.ttl = timedelta(days=ttl_days)
        self.now = now

    def get(self, item_type: str, tmdb_id) -> dict | None:
        """The stored payload when it is still fresh, else ``None``."""
        try:
            row = self.conn.execute(
                "SELECT payload, fetched_at FROM tmdb_cache"
                " WHERE item_type = ? AND tmdb_id = ?",
                (item_type, str(tmdb_id)),
            ).fetchone()
        except sqlite3.Error:
            return None  # a read-only or older database still detects
        if row is None:
            return None
        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if self.now() - fetched_at >= self.ttl:
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, item_type: str, tmdb_id, payload: dict) -> None:
        """Store one answer; a failure to store it never fails the scan."""
        owned = not self.conn.in_transaction
        try:
            self.conn.execute(
                "INSERT INTO tmdb_cache (item_type, tmdb_id, payload, fetched_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(item_type, tmdb_id) DO UPDATE SET"
                " payload = excluded.payload, fetched_at = excluded.fetched_at",
                (item_type, str(tmdb_id), json.dumps(payload, separators=(",", ":")),
                 self.now().isoformat()),
            )
        except sqlite3.Error:
            logger.warning("enrichment_tmdb_cache_not_stored")
            return
        if owned:
            # Part of the caller's transaction when there is one, so the
            # cache commits (or rolls back) with the work that filled it.
            self.conn.commit()


class TmdbEnricher:
    """Resolve by synced external id, never a potentially ambiguous title search.

    Answers are cached across scans in ``tmdb_cache`` (#74) and once per scan
    in memory, so one answer serves every user of it inside a scan too.
    Seasons inherit their series' identity. Sonarr's known status has
    precedence, and a series Sonarr already answered is not read at all when
    TMDB has nothing detection uses left to add. TMDB supplies a missing
    status and ratings, not an override of a known Plex rating (the detection
    engine enforces that final precedence).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        tmdb: TmdbClient | None = None,
        cache: TmdbCache | None = None,
    ) -> None:
        self.conn = conn
        self.tmdb = tmdb
        self.cache = cache if cache is not None else TmdbCache(conn)
        self._cache: dict[tuple[str, object], object] = {}

    def _item(self, rating_key):
        row = self.conn.execute("SELECT * FROM media_items WHERE plex_rating_key=?", (rating_key,)).fetchone()
        if row is not None and row["item_type"] == "season":
            row = self.conn.execute("SELECT * FROM media_items WHERE plex_rating_key=?",
                                    (row["parent_rating_key"],)).fetchone()
        return row

    def _model(self, item_type: str):
        return TmdbMovie if item_type == "movie" else TmdbShow

    def _stored(self, item_type: str, tmdb_id):
        """The answer one earlier scan stored, or None when it is not fresh."""
        payload = self.cache.get(item_type, tmdb_id)
        if payload is None:
            return None
        try:
            return self._model(item_type).model_validate(payload)
        except ValidationError:
            return None

    def _details(self, row):
        if row is None or not row["tmdb_id"] or self.tmdb is None:
            return None
        key = (row["item_type"], row["tmdb_id"])
        if key in self._cache:
            return self._cache[key]
        stored = self._stored(*key)
        if stored is not None:
            self._cache[key] = stored
            return stored
        try:
            details = (self.tmdb.movie(key[1]) if key[0] == "movie"
                       else self.tmdb.tv(key[1]))
        except Exception as error:
            self._cache[key] = None
            logger.warning("enrichment_tmdb_failed", extra={"error_type": type(error).__name__})
            return None
        self._cache[key] = details
        self.cache.put(key[0], key[1], details.model_dump())
        return details

    @staticmethod
    def _tmdb_adds_facts(row, status: SeriesStatus) -> bool:
        """Whether TMDB still holds a fact detection reads (#74).

        A rating for an item Plex stores none for (the high-rating
        protection), and the year an ended or cancelled reason names. With
        both known from the synced facts, asking would add nothing.
        """
        return row["rating"] is None or status in _YEAR_STATUSES

    def series_status(self, title: str, plex_rating_key: str) -> SeriesStatusInfo | None:
        row = self._item(plex_rating_key)
        if row is None or row["item_type"] != "series":
            return None
        status = _STATUS.get((row["production_status"] or "").casefold(), SeriesStatus.UNKNOWN)
        if status is not SeriesStatus.UNKNOWN and not self._tmdb_adds_facts(row, status):
            return SeriesStatusInfo(status=status)
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


def build_enrichers(
    conn: sqlite3.Connection,
    *,
    tmdb: TmdbClient | None = None,
    cache: TmdbCache | None = None,
) -> Enrichers:
    """Build both existing detection protocols over one shared fact cache."""
    provider = TmdbEnricher(conn, tmdb, cache=cache)
    return Enrichers(series_status=provider, rating=provider)
