"""The detection engine: pure computation over the synced database (S3).

The engine takes a SQLite connection filled by ``PlexSync``, optional
enrichers, a clock and the settings, and returns typed candidates. It makes
no network calls: external facts arrive only through the injected
protocols. Results are based on what is synced, never on assumptions about
users whose history is missing (S2).
"""

from __future__ import annotations

import calendar
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from almali_library_curator import library_scope
from almali_library_curator.config import Settings
from almali_library_curator.detection import text
from almali_library_curator.detection.enrichers import (
    Enrichers,
    SeriesStatus,
    SeriesStatusInfo,
)
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from almali_library_curator.i18n import DEFAULT_LOCALE
from almali_library_curator.plex.models import WATCHED_RATIO
from almali_library_curator.protection.models import ProtectionOutcome

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

# S3 order of the categories, for stable match ordering inside a candidate.
CATEGORY_ORDER: dict[Category, int] = {
    category: index for index, category in enumerate(Category)
}


def counts_as_watched(event: PlayEvent) -> bool:
    """S3: marked watched in Plex, or played past 90% in history."""
    return event.watched_flag or (
        event.duration_ms > 0 and event.position_ms >= WATCHED_RATIO * event.duration_ms
    )


def add_months(moment: datetime, months: int) -> datetime:
    """The same clock time ``months`` later, clamped to the month's end."""
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def is_past_threshold(now: datetime, reference: datetime | None, months: int) -> bool:
    """True when ``now`` is strictly past ``reference + months``.

    "More than N months" means strictly past: at exactly N months the
    category does not match yet.
    """
    if reference is None:
        return False
    return now > add_months(reference, months)


class Protector(Protocol):
    """Anything that filters protected items out of a scan (S4).

    :class:`almali_library_curator.protection.ProtectionEngine` implements
    it; the engine keeps no import of the protection engine itself, so
    both packages import cleanly in either order.
    """

    def protect(self, candidates: Sequence[Candidate]) -> ProtectionOutcome: ...


@dataclass(frozen=True)
class PlayEvent:
    """One synced play event on an attributed episode or movie."""

    user_id: int
    user_name: str
    rating_key: str
    watched_flag: bool
    played_at: datetime | None
    position_ms: int
    duration_ms: int
    season_number: int | None = None
    sequence: int = 0


@dataclass(frozen=True)
class EpisodeInfo:
    """One aired episode of a series, as synced.

    ``added_at`` is the newest dated addition for the episode's season (the
    season row or one of its episodes): the #70 added-at floor.
    """

    rating_key: str
    season_number: int
    size_bytes: int
    added_at: datetime | None = None


@dataclass(frozen=True)
class ItemRow:
    """A series or movie row of ``media_items``."""

    id: int
    plex_rating_key: str
    title: str
    item_type: str
    added_at: datetime | None
    rating: float | None
    size_bytes: int


@dataclass(frozen=True)
class Detection:
    """One category match on one item, before candidates are grouped."""

    item: ItemRow
    unit: Unit
    seasons: tuple[int, ...]
    size_bytes: int
    match: Match
    facts: dict


@dataclass
class SeriesAnalysis:
    """Everything the synced database knows about one series."""

    item: ItemRow
    episodes: list[EpisodeInfo]
    events: list[PlayEvent]
    watched_keys_by_user: dict[int, set[str]] = field(default_factory=dict)

    @property
    def seasons(self) -> list[int]:
        return sorted({episode.season_number for episode in self.episodes})

    @property
    def followers(self) -> list[tuple[int, str]]:
        """Users who played at least one episode, in user order."""
        seen: dict[int, str] = {}
        for event in self.events:
            seen.setdefault(event.user_id, event.user_name)
        return sorted(seen.items())

    @property
    def last_event(self) -> PlayEvent | None:
        with_time = [event for event in self.events if event.played_at is not None]
        if not with_time:
            return None
        return max(with_time, key=lambda event: (event.played_at, event.sequence))

    def last_event_of_user(self, user_id: int) -> PlayEvent | None:
        with_time = [
            event
            for event in self.events
            if event.user_id == user_id and event.played_at is not None
        ]
        if not with_time:
            return None
        return max(with_time, key=lambda event: (event.played_at, event.sequence))

    def episode_keys_of(self, season: int) -> list[str]:
        return [
            episode.rating_key
            for episode in self.episodes
            if episode.season_number == season
        ]

    def size_of_seasons(self, seasons: tuple[int, ...]) -> int:
        wanted = set(seasons)
        return sum(
            episode.size_bytes
            for episode in self.episodes
            if episode.season_number in wanted
        )

    @property
    def total_size(self) -> int:
        return sum(episode.size_bytes for episode in self.episodes)


class DetectionEngine:
    """Finds the six S3 categories and builds their typed candidates."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        enrichers: Enrichers | None = None,
        clock: Clock,
        settings: Settings,
        protector: Protector | None = None,
    ) -> None:
        self.conn = conn
        self.enrichers = enrichers or Enrichers()
        self.clock = clock
        self.settings = settings
        self.protector = protector
        self.locale = settings.locale or DEFAULT_LOCALE
        self._status_cache: dict[str, SeriesStatusInfo | None] = {}
        self._users: dict[int, str] | None = None
        self._viewer_count = 0

    # -- public interface -------------------------------------------------

    def scan(self) -> list[Candidate]:
        """Run every category and return the candidates.

        With a protector injected, protected items never come out of the
        scan as candidates (S4); without one, behaviour is unchanged.
        """
        if self.protector is None:
            return self._detect()
        return list(self.scan_with_protection().candidates)

    def scan_with_protection(self) -> ProtectionOutcome:
        """One scan returning the surviving candidates and the protected
        items together: the single source of truth for the Protected
        view and the scan orchestrator."""
        return self._protected(self._detect())

    def scan_items(self, media_item_ids: Iterable[int]) -> ProtectionOutcome:
        """The full scan restricted to ``media_item_ids``.

        It answers exactly what :meth:`scan_with_protection` answers for
        those items, in the same order, through the same users,
        thresholds, enrichers, library scope and protection, while it
        reads only those items' rows and play events (#5, #6). Unknown
        identifiers answer nothing.
        """
        only = frozenset(int(identifier) for identifier in media_item_ids)
        return self._protected(self._detect(only=only))

    def _protected(self, candidates: list[Candidate]) -> ProtectionOutcome:
        if self.protector is None:
            return ProtectionOutcome(candidates=tuple(candidates), protected=())
        return self.protector.protect(candidates)

    def _detect(self, *, only: frozenset[int] | None = None) -> list[Candidate]:
        """The raw scan, before any protection is applied.

        ``only`` restricts it to those media item ids; ``None`` scans
        every item.
        """
        # Reused engines must see user changes between detection passes,
        # while every candidate in this pass shares the same users query.
        self._users = None
        # #34: series-status answers get the same pass scope: one enricher
        # ask per series within this pass, fresh answers on the next one.
        self._status_cache = {}
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        # S14 rule 4: items of deselected libraries stay in the database
        # but are never detected. ``None`` means no restriction.
        scope = library_scope.curated_library_ids(self.conn, self.settings)

        users_by_id = self._load_users()
        episode_index = self._load_episode_index(scope, only)
        movie_keys = self._load_movie_keys(scope, only)
        events = self._load_events(users_by_id, episode_index, movie_keys, only)

        # #4: group once, so the per-item work no longer rescans the whole
        # history and the whole episode index for every item. Each group
        # keeps the original order, which the tie-breaks rely on.
        episodes_by_series: dict[str, list[EpisodeInfo]] = {}
        for series_key, episode in episode_index.values():
            episodes_by_series.setdefault(series_key, []).append(episode)
        events_by_series: dict[str, list[PlayEvent]] = {}
        events_by_key: dict[str, list[PlayEvent]] = {}
        for event in events:
            entry = episode_index.get(event.rating_key)
            if entry is not None:
                events_by_series.setdefault(entry[0], []).append(event)
            events_by_key.setdefault(event.rating_key, []).append(event)

        detections: list[Detection] = []
        for item in self._load_items("series", scope, only):
            series_events = events_by_series.get(item.plex_rating_key, [])
            analysis = SeriesAnalysis(
                item=item,
                episodes=episodes_by_series.get(item.plex_rating_key, []),
                events=series_events,
                watched_keys_by_user=self._watched_map(series_events),
            )
            detections.extend(self._series_detections(analysis, now))
        for item in self._load_items("movie", scope, only):
            movie_events = events_by_key.get(item.plex_rating_key, [])
            detections.extend(self._movie_detections(item, movie_events, now))

        candidates = self._grouped_candidates(detections)
        candidates.sort(key=lambda candidate: (-candidate.size_bytes, candidate.title))
        logger.info(
            "detection_scan_finished",
            extra={"candidates": len(candidates)},
        )
        return candidates

    # -- loading ----------------------------------------------------------

    def _load_users(self) -> dict[int, str]:
        """The users whose activity counts; excluded users count for nothing.

        The list is read once per scan: the candidates ask for the number of
        users again and again, and the users do not change during a scan.
        """
        if self._users is not None:
            return self._users
        excluded_names = {
            name.strip().lower() for name in self.settings.excluded_users if name.strip()
        }
        users: dict[int, str] = {}
        viewers = 0
        # #122: Plex lists every account the server ever shared with, so
        # the never-watched sentence counts only those with a recorded play.
        # The same statement answers it, keeping one users read per pass.
        for row in self.conn.execute(
            "SELECT id, title, excluded,"
            " EXISTS (SELECT 1 FROM play_events WHERE play_events.user_id = users.id)"
            " AS has_play FROM users ORDER BY id"
        ):
            if row["excluded"]:
                continue
            if (row["title"] or "").strip().lower() in excluded_names:
                continue
            users[int(row["id"])] = row["title"]
            viewers += 1 if row["has_play"] else 0
        self._users = users
        self._viewer_count = viewers
        return users

    def _load_episode_index(
        self, scope: tuple[int, ...] | None, only: frozenset[int] | None = None
    ) -> dict[str, tuple[str, EpisodeInfo]]:
        """Episodes of series inside the curated scope (S14 rule 4)."""
        if (scope is not None and not scope) or (only is not None and not only):
            return {}
        sql = (
            "SELECT e.plex_rating_key, e.series_rating_key, e.season_number,"
            " e.file_size_bytes, e.added_at AS episode_added_at,"
            " s.added_at AS season_added_at"
            " FROM episodes e"
            " JOIN media_items m ON m.plex_rating_key = e.series_rating_key"
            " LEFT JOIN media_items s ON s.id = e.media_item_id"
        )
        clauses, params = self._restriction("m.", scope, only)
        clauses.append("e.plex_present = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        index: dict[str, tuple[str, EpisodeInfo]] = {}
        for row in self.conn.execute(sql, params):
            added = [
                moment
                for moment in (
                    self._parse_iso(row["episode_added_at"]),
                    self._parse_iso(row["season_added_at"]),
                )
                if moment is not None
            ]
            index[row["plex_rating_key"]] = (
                row["series_rating_key"],
                EpisodeInfo(
                    rating_key=row["plex_rating_key"],
                    season_number=row["season_number"] or 0,
                    size_bytes=row["file_size_bytes"] or 0,
                    added_at=max(added) if added else None,
                ),
            )
        return index

    @staticmethod
    def _restriction(
        prefix: str, scope: tuple[int, ...] | None, only: frozenset[int] | None
    ) -> tuple[list[str], list]:
        """SQL clauses for the curated scope and the item restriction."""
        clauses: list[str] = [f"{prefix}plex_present = 1"]
        params: list = []
        if scope is not None:
            clauses.append(f"{prefix}library_id IN ({', '.join('?' for _ in scope)})")
            params += list(scope)
        if only is not None:
            ordered = sorted(only)
            clauses.append(f"{prefix}id IN ({', '.join('?' for _ in ordered)})")
            params += ordered
        return clauses, params

    def _load_movie_keys(
        self, scope: tuple[int, ...] | None, only: frozenset[int] | None = None
    ) -> set[str]:
        """Rating keys of synced movies, so their play events attribute too."""
        if (scope is not None and not scope) or (only is not None and not only):
            return set()
        sql = "SELECT plex_rating_key FROM media_items WHERE item_type = 'movie'"
        clauses, params = self._restriction("", scope, only)
        for clause in clauses:
            sql += f" AND {clause}"
        return {row["plex_rating_key"] for row in self.conn.execute(sql, params)}

    def _load_events(
        self,
        users_by_id: dict[int, str],
        episode_index: dict[str, tuple[str, EpisodeInfo]],
        movie_keys: set[str],
        only: frozenset[int] | None = None,
    ) -> list[PlayEvent]:
        sql = (
            "SELECT user_id, plex_rating_key, watched, played_at, position_ms, duration_ms"
            " FROM play_events"
        )
        params: list = []
        if only is not None:
            # Only the restricted items' events, read through the
            # rating-key index. ``sequence`` stays a relative order, which
            # is all the tie-breaks compare inside one item.
            keys = sorted(set(episode_index) | movie_keys)
            if not keys:
                return []
            sql += " WHERE plex_rating_key IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(keys))
        sql += " ORDER BY id"
        events: list[PlayEvent] = []
        for sequence, row in enumerate(self.conn.execute(sql, params)):
            if row["user_id"] not in users_by_id:
                continue
            episode = episode_index.get(row["plex_rating_key"])
            if episode is None and row["plex_rating_key"] not in movie_keys:
                # Not attributable to anything synced: never a basis for a fact.
                continue
            events.append(
                PlayEvent(
                    user_id=int(row["user_id"]),
                    user_name=users_by_id[int(row["user_id"])],
                    rating_key=row["plex_rating_key"],
                    watched_flag=bool(row["watched"]),
                    played_at=self._parse_iso(row["played_at"]),
                    position_ms=row["position_ms"] or 0,
                    duration_ms=row["duration_ms"] or 0,
                    season_number=episode[1].season_number if episode else None,
                    sequence=sequence,
                )
            )
        return events

    @staticmethod
    def _watched_map(events: list[PlayEvent]) -> dict[int, set[str]]:
        """Episode keys that count as watched per user, per the S3 definition."""
        watched: dict[int, set[str]] = {}
        for event in events:
            if counts_as_watched(event):
                watched.setdefault(event.user_id, set()).add(event.rating_key)
        return watched

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    def _load_items(
        self,
        item_type: str,
        scope: tuple[int, ...] | None,
        only: frozenset[int] | None = None,
    ) -> list[ItemRow]:
        """Rows of one type inside the curated scope (S14 rule 4)."""
        if (scope is not None and not scope) or (only is not None and not only):
            return []
        sql = (
            "SELECT id, plex_rating_key, title, added_at, rating, file_size_bytes"
            " FROM media_items WHERE item_type = ?"
        )
        clauses, extra = self._restriction("", scope, only)
        for clause in clauses:
            sql += f" AND {clause}"
        params: list = [item_type, *extra]
        sql += " ORDER BY id"
        rows = self.conn.execute(sql, params)
        return [
            ItemRow(
                id=int(row["id"]),
                plex_rating_key=row["plex_rating_key"],
                title=row["title"],
                item_type=item_type,
                added_at=self._parse_iso(row["added_at"]),
                rating=row["rating"],
                size_bytes=row["file_size_bytes"] or 0,
            )
            for row in rows
        ]

    # -- category checks ----------------------------------------------------

    def _series_detections(self, analysis: SeriesAnalysis, now: datetime) -> list[Detection]:
        detections: list[Detection] = []
        stalled = self._stalled_series(analysis, now)
        if stalled is not None:
            seasons, match, facts = stalled
            detections.append(
                Detection(
                    item=analysis.item,
                    unit=Unit.SEASONS,
                    seasons=seasons,
                    size_bytes=analysis.size_of_seasons(seasons),
                    match=match,
                    facts=facts,
                )
            )
        never = self._never_watched(analysis.item, analysis.events, now, unit="series")
        if never is not None:
            match, facts = never
            detections.append(
                Detection(
                    item=analysis.item,
                    unit=Unit.SERIES,
                    seasons=(),
                    size_bytes=analysis.total_size,
                    match=match,
                    facts=facts,
                )
            )
        cancelled = self._cancelled_no_viewers(analysis, now)
        if cancelled is not None:
            match, facts = cancelled
            detections.append(
                Detection(
                    item=analysis.item,
                    unit=Unit.SERIES,
                    seasons=(),
                    size_bytes=analysis.total_size,
                    match=match,
                    facts=facts,
                )
            )
        finished = self._seasons_everyone_finished(analysis, now)
        if finished is not None:
            seasons, match, facts = finished
            detections.append(
                Detection(
                    item=analysis.item,
                    unit=Unit.SEASONS,
                    seasons=seasons,
                    size_bytes=analysis.size_of_seasons(seasons),
                    match=match,
                    facts=facts,
                )
            )
        # #135: only a series the other categories do not already cover.
        running = self._running_series_finished_seasons(
            analysis,
            now,
            stalled=stalled is not None,
            everyone_finished=finished is not None,
        )
        if running is not None:
            seasons, match, facts = running
            detections.append(
                Detection(
                    item=analysis.item,
                    unit=Unit.SEASONS,
                    seasons=seasons,
                    size_bytes=analysis.size_of_seasons(seasons),
                    match=match,
                    facts=facts,
                )
            )
        return detections

    def _movie_detections(
        self, item: ItemRow, events: list[PlayEvent], now: datetime
    ) -> list[Detection]:
        detections: list[Detection] = []
        never = self._never_watched(item, events, now, unit="movie")
        if never is not None:
            match, facts = never
            detections.append(
                Detection(
                    item=item,
                    unit=Unit.MOVIE,
                    seasons=(),
                    size_bytes=item.size_bytes,
                    match=match,
                    facts=facts,
                )
            )
        rewatched = self._movies_not_rewatched(item, events, now)
        if rewatched is not None:
            match, facts = rewatched
            detections.append(
                Detection(
                    item=item,
                    unit=Unit.MOVIE,
                    seasons=(),
                    size_bytes=item.size_bytes,
                    match=match,
                    facts=facts,
                )
            )
        return detections

    def _stalled_series(
        self, analysis: SeriesAnalysis, now: datetime
    ) -> tuple[tuple[int, ...], Match, dict] | None:
        """Someone watched part of the show, later seasons sat with no play."""
        months = self.settings.thresholds.stalled_series_months
        played_seasons = {
            event.season_number for event in analysis.events if event.season_number is not None
        }
        if not played_seasons:
            return None
        # Only the seasons after the point the watching reached are "later".
        # #70: a season (or one of its episodes) added inside the stalled
        # window has not had time to be watched, so it is not stalled. The
        # newest dated addition is the season's floor; ``stalled_series_months``
        # is reused, so no new setting is needed.
        earliest_played = min(played_seasons)
        unopened: list[int] = []
        for season in analysis.seasons:
            if season in played_seasons or season <= earliest_played:
                continue
            added = max(
                (
                    episode.added_at
                    for episode in analysis.episodes
                    if episode.season_number == season and episode.added_at is not None
                ),
                default=None,
            )
            if added is not None and not is_past_threshold(now, added, months):
                continue
            unopened.append(season)
        last = analysis.last_event
        if not unopened or last is None or last.played_at is None:
            return None
        if not is_past_threshold(now, last.played_at, months):
            return None

        variant = "singular" if len(unopened) == 1 else "plural"
        parts = (
            text.reason_part(
                f"detection.reason.stalled.{variant}",
                name=last.user_name,
                season=last.season_number,
                date=last.played_at.isoformat(),
                unopened=list(unopened),
            ),
        )
        reason = text.render_reason(self.locale, parts)
        facts = {
            "last_played_at": last.played_at.isoformat(),
            "last_play_user": last.user_name,
            "last_play_season": last.season_number,
            "unopened_seasons": unopened,
        }
        return tuple(unopened), Match(Category.STALLED_SERIES, reason, parts), facts

    def _never_watched(
        self, item: ItemRow, events: list[PlayEvent], now: datetime, *, unit: str
    ) -> tuple[Match, dict] | None:
        """No user has played any part since it was added."""
        months = self.settings.thresholds.never_watched_months
        if events:
            return None
        if not is_past_threshold(now, item.added_at, months):
            return None
        user_count = self._user_count()
        # #122 S2: with nobody who ever played, "any of the 0 users" says
        # nothing; the sentence drops the number and the item still shows.
        key = f"detection.reason.never_watched.{unit}"
        parts = (
            text.reason_part(key, date=item.added_at.isoformat(), users=user_count)
            if user_count
            else text.reason_part(f"{key}_no_plays", date=item.added_at.isoformat()),
        )
        reason = text.render_reason(self.locale, parts)
        facts = {
            "added_at": item.added_at.isoformat(),
            "user_count": user_count,
        }
        return Match(Category.NEVER_WATCHED, reason, parts), facts

    def _cancelled_no_viewers(
        self, analysis: SeriesAnalysis, now: datetime
    ) -> tuple[Match, dict] | None:
        """An ended or cancelled series with no user mid-show."""
        months = self.settings.thresholds.cancelled_no_viewers_months
        info = self._series_status(analysis.item)
        if info is None or info.status not in (SeriesStatus.ENDED, SeriesStatus.CANCELLED):
            return None
        mid_show = self._mid_show_users(analysis, now)
        if mid_show:
            return None
        last = analysis.last_event
        reference = last.played_at if last is not None else analysis.item.added_at
        if not is_past_threshold(now, reference, months):
            return None

        if last is not None and last.played_at is not None:
            key = (
                "detection.reason.cancelled.played"
                if info.ended_year is not None
                else "detection.reason.cancelled.played_no_year"
            )
            params = {
                "year": info.ended_year,
                "date": last.played_at.isoformat(),
            }
        else:
            key = (
                "detection.reason.cancelled.never_played"
                if info.ended_year is not None
                else "detection.reason.cancelled.never_played_no_year"
            )
            params = {"year": info.ended_year}
        parts = (text.reason_part(key, **params),)
        clause = text.render_reason(self.locale, parts)
        facts = {
            "status": info.status.value,
            "ended_year": info.ended_year,
            "last_played_at": last.played_at.isoformat() if last is not None else None,
            "mid_show_users": mid_show,
        }
        return Match(Category.CANCELLED_NO_VIEWERS, clause, parts), facts

    def _mid_show_users(self, analysis: SeriesAnalysis, now: datetime) -> list[str]:
        """Followers mid-show: some but not all aired episodes, recent play."""
        months = self.settings.thresholds.stalled_series_months
        total = len(analysis.episodes)
        names: list[str] = []
        for user_id, name in analysis.followers:
            watched = len(analysis.watched_keys_by_user.get(user_id, ()))
            if not 0 < watched < total:
                continue
            user_last = analysis.last_event_of_user(user_id)
            if user_last is None or user_last.played_at is None:
                continue
            if not is_past_threshold(now, user_last.played_at, months):
                names.append(name)
        return names

    def _seasons_everyone_finished(
        self, analysis: SeriesAnalysis, now: datetime
    ) -> tuple[tuple[int, ...], Match, dict] | None:
        """Seasons every follower finished, with later seasons still pending."""
        months = self.settings.thresholds.everyone_finished_months
        followers = analysis.followers
        if not followers:
            return None
        finished = self._finished_prefix_seasons(analysis)
        if not finished:
            return None
        pending = [season for season in analysis.seasons if season > finished[-1]]
        if not pending:
            return None
        finishing_plays = [
            event.played_at
            for event in analysis.events
            if event.season_number in finished and event.played_at is not None
        ]
        if not finishing_plays or not is_past_threshold(now, max(finishing_plays), months):
            return None

        names = [name for _, name in followers]
        first = self._followers_finished_part(names, list(finished))
        pending_variant = "singular" if len(pending) == 1 else "plural"
        second = text.reason_part(
            f"detection.reason.pending.{pending_variant}",
            seasons=list(pending),
        )
        parts = (first, second)
        reason = text.render_reason(self.locale, parts)
        facts = {
            "followers": names,
            "finished_seasons": finished,
            "pending_seasons": pending,
            "all_finished_at": max(finishing_plays).isoformat(),
        }
        return tuple(finished), Match(Category.SEASONS_EVERYONE_FINISHED, reason, parts), facts

    def _running_series_finished_seasons(
        self,
        analysis: SeriesAnalysis,
        now: datetime,
        *,
        stalled: bool,
        everyone_finished: bool,
    ) -> tuple[tuple[int, ...], Match, dict] | None:
        """#135: the finished seasons of a series that is still running.

        Only the seasons before the latest one everyone finished are
        proposed: the latest finished season stays for a rewatch before the
        new one, and so does every season after it, including the one
        someone is still watching (the finished seasons are a prefix, so the
        first unfinished season and everything newer are kept). Ended,
        cancelled and stalled series, and a series the everyone-finished
        category already proposes, keep their own row.
        """
        months = self.settings.thresholds.running_series_finished_seasons_months
        info = self._series_status(analysis.item)
        if info is not None and info.status in (
            SeriesStatus.ENDED,
            SeriesStatus.CANCELLED,
        ):
            # Covered by the cancelled-no-viewers category, and a finished
            # series is not what this category is about.
            return None
        if stalled:
            # The stalled category already covers the later unopened seasons.
            return None
        if everyone_finished:
            # One season row per series: when the everyone-finished category
            # already proposes this series' seasons, that row is the one.
            return None
        followers = analysis.followers
        if not followers:
            return None
        finished = self._finished_prefix_seasons(analysis)
        if len(finished) < 2:
            # Nothing older than the latest finished season to propose.
            return None
        proposed = tuple(finished[:-1])
        kept = finished[-1]
        finishing_plays = [
            event.played_at
            for event in analysis.events
            if event.season_number in proposed and event.played_at is not None
        ]
        if not finishing_plays or not is_past_threshold(
            now, max(finishing_plays), months
        ):
            return None

        names = [name for _, name in followers]
        parts = (
            self._followers_finished_part(names, list(proposed)),
            text.reason_part("detection.reason.running.stays", season=kept),
        )
        reason = text.render_reason(self.locale, parts)
        facts = {
            "followers": names,
            "finished_seasons": list(proposed),
            "kept_season": kept,
            "all_finished_at": max(finishing_plays).isoformat(),
            "status": info.status.value if info is not None else SeriesStatus.UNKNOWN.value,
        }
        return (
            proposed,
            Match(Category.RUNNING_SERIES_FINISHED_SEASONS, reason, parts),
            facts,
        )

    def _followers_finished_part(self, names: list[str], seasons: list[int]):
        """The "everyone finished these seasons" clause, one, two or many.

        Both seasons categories open their reason with it, so the wording and
        the raw parameters stay in one place.
        """
        if len(names) == 1:
            return text.reason_part(
                "detection.reason.everyone_finished.one",
                name=names[0],
                seasons=seasons,
            )
        if len(names) == 2:
            return text.reason_part(
                "detection.reason.everyone_finished.two",
                name1=names[0],
                name2=names[1],
                seasons=seasons,
            )
        return text.reason_part(
            "detection.reason.everyone_finished.many",
            names=names,
            seasons=seasons,
        )

    def _finished_prefix_seasons(self, analysis: SeriesAnalysis) -> list[int]:
        """The seasons every follower finished, up to the first gap.

        The single computation behind both seasons categories: a season
        counts as finished when every counted user watched all its episodes
        (S3's watched rule, applied in ``SeriesAnalysis``), and the prefix
        ends at the first season one of them did not finish.
        """
        followers = analysis.followers
        finished: list[int] = []
        if not followers:
            return finished
        for season in analysis.seasons:
            keys = set(analysis.episode_keys_of(season))
            if not keys:
                break
            if all(
                analysis.watched_keys_by_user.get(user_id, set()) >= keys
                for user_id, _ in followers
            ):
                finished.append(season)
            else:
                break
        return finished

    def _movies_not_rewatched(
        self, item: ItemRow, events: list[PlayEvent], now: datetime
    ) -> tuple[Match, dict] | None:
        """Watched at least once, nobody played it since."""
        months = self.settings.thresholds.movies_not_rewatched_months
        watched_events = [event for event in events if counts_as_watched(event)]
        if not watched_events:
            return None
        last_play = max(
            (event.played_at for event in events if event.played_at is not None),
            default=None,
        )
        if not is_past_threshold(now, last_play, months):
            return None
        last_watched = max(
            (event for event in watched_events if event.played_at is not None),
            key=lambda event: (event.played_at, event.sequence),
        )
        watchers: dict[int, str] = {}
        for event in sorted(watched_events, key=lambda event: event.sequence):
            watchers.setdefault(event.user_id, event.user_name)
        names = list(watchers.values())
        date = last_watched.played_at.isoformat()
        if len(names) == 1 and len(watched_events) == 1:
            variant_key = "detection.reason.not_rewatched.once"
            params: dict[str, object] = {"name": names[0], "date": date}
        elif len(names) == 1 and len(watched_events) == 2:
            variant_key = "detection.reason.not_rewatched.twice"
            params = {"name": names[0], "date": date}
        else:
            variant_key = "detection.reason.not_rewatched.times"
            params = {
                "count": len(watched_events),
                "names": names,
                "date": date,
            }
        parts = (text.reason_part(variant_key, **params),)
        reason = text.render_reason(self.locale, parts)
        facts = {
            "watch_count": len(watched_events),
            "watchers": names,
            "last_played_at": last_play.isoformat() if last_play else None,
        }
        return Match(Category.MOVIES_NOT_REWATCHED, reason, parts), facts

    # -- helpers ------------------------------------------------------------

    def _user_count(self) -> int:
        """Included users with at least one recorded play (#122), for the
        never-watched sentence.

        Read with the users of this pass, so no further query is needed.
        """
        self._load_users()
        return self._viewer_count

    def _grouped_candidates(self, detections: list[Detection]) -> list[Candidate]:
        """One appearance per item and unit, with every matching reason."""
        groups: dict[tuple[int, str, tuple[int, ...]], list[Detection]] = {}
        for detection in detections:
            key = (detection.item.id, detection.unit.value, detection.seasons)
            groups.setdefault(key, []).append(detection)
        candidates = []
        for group in groups.values():
            ordered = sorted(group, key=lambda d: CATEGORY_ORDER[d.match.category])
            first = ordered[0]
            matches = tuple(detection.match for detection in ordered)
            facts = {
                "matches": {
                    detection.match.category.value: detection.facts for detection in ordered
                }
            }
            candidates.append(
                Candidate(
                    media_item_id=first.item.id,
                    plex_rating_key=first.item.plex_rating_key,
                    title=self._title(first),
                    unit=first.unit,
                    seasons=first.seasons,
                    matches=matches,
                    size_bytes=first.size_bytes,
                    rating=self._item_rating(first.item),
                    facts=facts,
                )
            )
        return candidates

    @staticmethod
    def _title(detection: Detection) -> str:
        if detection.unit is Unit.SEASONS:
            return f"{detection.item.title} {text.season_runs(list(detection.seasons))}"
        return detection.item.title

    def _series_status(self, item: ItemRow) -> SeriesStatusInfo | None:
        """Ask the enricher once per series; absent sources answer nothing."""
        if item.plex_rating_key in self._status_cache:
            return self._status_cache[item.plex_rating_key]
        info = None
        if self.enrichers.series_status is not None:
            info = self.enrichers.series_status.series_status(
                item.title, item.plex_rating_key
            )
        self._status_cache[item.plex_rating_key] = info
        return info

    def _item_rating(self, item: ItemRow) -> float | None:
        """The number the Rating source setting chooses for this item.

        The sync already stored the chosen Plex number. ``highest`` also
        compares that number with the TMDB fallback, so it reads the
        fallback even when Plex stores a rating; the other choices only ask
        TMDB when Plex stores none, exactly as before #128.
        """
        highest = self.settings.rating_source == "highest"
        fallback = None
        if item.rating is None or highest:
            fallback = self._fallback_rating(item)
        if highest:
            values = [value for value in (item.rating, fallback) if value is not None]
            return max(values) if values else None
        return item.rating if item.rating is not None else fallback

    def _fallback_rating(self, item: ItemRow) -> float | None:
        """The enrichers' rating for an item Plex does not rate."""
        if item.item_type == "series":
            info = self._series_status(item)
            if info is not None and info.rating is not None:
                return info.rating
        if self.enrichers.rating is not None:
            return self.enrichers.rating.rating(item.title, item.item_type, item.plex_rating_key)
        return None

