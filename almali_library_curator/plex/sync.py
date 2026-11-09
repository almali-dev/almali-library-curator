"""Sync what Plex knows into the local database."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from almali_library_curator import library_scope
from almali_library_curator.plex.client import PlexClient, PlexNotFoundError
from almali_library_curator.plex.models import PlexAccount, PlexMetadata, is_watched

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    """How much a sync moved.

    ``libraries`` counts the sections seen; ``curated_libraries`` counts the
    sections actually synced (S14 rule 2).
    """

    libraries: int = 0
    curated_libraries: int = 0
    media_items: int = 0
    episodes: int = 0
    users: int = 0
    play_events: int = 0


def epoch_to_iso(value: int | None) -> str | None:
    """Plex reports timestamps in seconds since the epoch."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def grouped_by_parent(items: list[PlexMetadata]) -> dict[str, list[PlexMetadata]]:
    """The items of a bulk read, grouped by the item they belong to.

    A section read answers the seasons of every series and the episodes of
    every season at once. Grouping them by their parent keeps the order the
    per-item reads answered, so the stored rows do not move.
    """
    grouped: dict[str, list[PlexMetadata]] = {}
    for item in items:
        grouped.setdefault(item.parentRatingKey or "", []).append(item)
    return grouped


class PlexSync:
    """Pulls the Plex library, the accounts and the watch history."""

    def __init__(
        self,
        client: PlexClient,
        conn: sqlite3.Connection,
        owner_title: str | None = None,
        curated_libraries: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.conn = conn
        self.owner_title = owner_title
        # S14: an empty selection means every library is curated.
        self.curated_libraries = tuple(curated_libraries)
        # S24: the rows of every table are collected and written in one
        # batch, instead of one statement per row.
        self._pending: dict[str, list[dict[str, object]]] = {
            "libraries": [],
            "media_items": [],
            "episodes": [],
            "users": [],
            "play_events": [],
        }
        # The id of every user, read once after the users are written.
        self._user_ids: dict[str, int] = {}

    def run(self) -> SyncSummary:
        """Read everything from Plex and store it. Safe to run twice.

        Every Plex section gets a ``libraries`` row, because that list is
        what the owner picks the curated scope from. Items, seasons and
        episodes are fetched only for the curated libraries, so an
        uncurated library produces no outbound request at all (S14 rule 2).
        """
        summary = SyncSummary()

        sections = self.client.sections()
        for section in sections:
            self._store_library(section)
            summary.libraries += 1
            if library_scope.library_is_curated(section.key, self.curated_libraries):
                summary.curated_libraries += 1
        library_ids = self._write_libraries()

        for section in sections:
            if not library_scope.library_is_curated(section.key, self.curated_libraries):
                continue
            item_type = "movie" if (section.type or "").lower() == "movie" else "show"
            items = self.client.section_items(section.key, item_type)
            seasons: list[PlexMetadata] = []
            episodes: list[PlexMetadata] = []
            if item_type == "show":
                # S23: the seasons and the episodes of the whole section are
                # read at once instead of one request per series and per
                # season. The rows stored are the same rows.
                seasons = self._seasons_of(section.key, items)
                episodes = self._episodes_of(section.key, seasons)
            seasons_by_series = grouped_by_parent(seasons)
            episodes_by_season = grouped_by_parent(episodes)
            for item in items:
                stored_type = "movie" if item_type == "movie" else "series"
                self._store_media_item(item, library_ids[section.key], stored_type)
                summary.media_items += 1
                if item_type != "show":
                    continue
                for season_item in seasons_by_series.get(item.ratingKey, []):
                    self._store_media_item(
                        season_item,
                        library_ids[section.key],
                        "season",
                        parent_key=item.ratingKey,
                    )
                    summary.media_items += 1
                    for episode_item in episodes_by_season.get(season_item.ratingKey, []):
                        self._store_episode(
                            episode_item,
                            season_item.ratingKey,
                            item.ratingKey,
                            season_item.index,
                        )
                        summary.episodes += 1

        for account in self.client.accounts():
            self._store_user(account)
            summary.users += 1
        self._write_users()

        for entry in self.client.history():
            if self._store_play_event(entry):
                summary.play_events += 1

        self._write_media_items()
        self._write_episodes()
        self._write_play_events()
        self.conn.commit()
        logger.info("plex_sync_finished", extra={"summary": summary.__dict__})
        return summary

    # -- reading ---------------------------------------------------------

    def _seasons_of(
        self, section_key: str, series_items: list[PlexMetadata]
    ) -> list[PlexMetadata]:
        """The seasons of a show section, read in one request.

        A server that does not know the bulk read (HTTP 404), or that
        answers it with nothing although the series report seasons, is read
        the old way, one series at a time, so the sync stores the same rows.
        """
        try:
            seasons = self.client.section_items(section_key, "season", include_guids=False)
        except PlexNotFoundError:
            logger.warning(
                "plex_bulk_seasons_unavailable", extra={"section": section_key}
            )
            seasons = []
        answered = {season.parentRatingKey for season in seasons}
        missing = [
            series_item
            for series_item in series_items
            if series_item.childCount and series_item.ratingKey not in answered
        ]
        if missing:
            logger.warning(
                "plex_bulk_seasons_incomplete", extra={"section": section_key}
            )
            for series_item in missing:
                seasons.extend(self.client.children(series_item.ratingKey, "season"))
        return seasons

    def _episodes_of(
        self, section_key: str, seasons: list[PlexMetadata]
    ) -> list[PlexMetadata]:
        """The episodes of a show section, read in one request.

        A server that does not know the bulk read (HTTP 404), or that
        answers it with nothing although the seasons report episodes, is
        read the old way, one season at a time.
        """
        try:
            episodes = self.client.section_items(section_key, "episode", include_guids=False)
        except PlexNotFoundError:
            logger.warning(
                "plex_bulk_episodes_unavailable", extra={"section": section_key}
            )
            episodes = []
        answered = {episode.parentRatingKey for episode in episodes}
        missing = [
            season_item
            for season_item in seasons
            if season_item.leafCount and season_item.ratingKey not in answered
        ]
        if missing:
            logger.warning(
                "plex_bulk_episodes_incomplete", extra={"section": section_key}
            )
            for season_item in missing:
                episodes.extend(self.client.children(season_item.ratingKey, "episode"))
        return episodes

    # -- storage ---------------------------------------------------------

    def _upsert_many(
        self, table: str, key_columns: tuple[str, ...], rows: list[dict[str, object]]
    ) -> None:
        """Write many rows of one table in one batch (S24).

        The rows of a table always carry the same columns, so the statement
        is built once and every row is written through it.
        """
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in key_columns
        )
        conflict = ", ".join(key_columns)
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )
        self.conn.executemany(
            statement, [[row[column] for column in columns] for row in rows]
        )

    def _write_libraries(self) -> dict[str, int]:
        """Write the libraries and read their ids in one go."""
        self._upsert_many("libraries", ("plex_key",), self._pending["libraries"])
        self._pending["libraries"] = []
        return {
            row["plex_key"]: int(row["id"])
            for row in self.conn.execute("SELECT id, plex_key FROM libraries")
        }

    def _write_media_items(self) -> None:
        self._upsert_many(
            "media_items", ("plex_rating_key",), self._pending["media_items"]
        )
        self._pending["media_items"] = []

    def _write_episodes(self) -> None:
        """Write the episodes, with the season of each one read once."""
        season_ids = {
            row["plex_rating_key"]: int(row["id"])
            for row in self.conn.execute(
                "SELECT id, plex_rating_key FROM media_items WHERE item_type = 'season'"
            )
        }
        rows = []
        for pending in self._pending["episodes"]:
            row = dict(pending)
            row["media_item_id"] = season_ids.get(row.pop("_season_key"))
            rows.append(row)
        self._upsert_many("episodes", ("plex_rating_key",), rows)
        self._pending["episodes"] = []

    def _write_users(self) -> None:
        """Write the users and read their ids in one go."""
        self._upsert_many("users", ("plex_user_id",), self._pending["users"])
        self._pending["users"] = []
        self._user_ids = {
            row["plex_user_id"]: int(row["id"])
            for row in self.conn.execute("SELECT id, plex_user_id FROM users")
        }

    def _write_play_events(self) -> None:
        self._upsert_many(
            "play_events",
            ("user_id", "plex_rating_key", "played_at", "position_ms"),
            self._pending["play_events"],
        )
        self._pending["play_events"] = []

    def _store_library(self, section) -> None:
        self._pending["libraries"].append(
            {
                "plex_key": section.key,
                "title": section.title,
                "section_type": section.type or "",
            }
        )

    def _store_media_item(
        self,
        item: PlexMetadata,
        library_id: int,
        item_type: str,
        parent_key: str | None = None,
    ) -> None:
        self._pending["media_items"].append(
            {
                "library_id": library_id,
                "plex_rating_key": item.ratingKey,
                "parent_rating_key": parent_key,
                "title": item.title,
                "year": item.year,
                **item.external_ids,
                "item_type": item_type,
                "season_number": item.index if item_type == "season" else None,
                "added_at": epoch_to_iso(item.addedAt),
                "last_played_at": epoch_to_iso(item.lastViewedAt),
                "rating": item.rating,
                "rating_source": "plex" if item.rating is not None else None,
                "file_path": self._mapped_path(item),
                "file_size_bytes": item.file_size,
                "updated_at": now_iso(),
            }
        )

    def _store_episode(
        self,
        item: PlexMetadata,
        season_key: str,
        series_key: str,
        season_number: int | None,
    ) -> None:
        self._pending["episodes"].append(
            {
                # The season of the episode is resolved when the batch is
                # written, once the seasons are stored.
                "_season_key": season_key,
                "media_item_id": None,
                "plex_rating_key": item.ratingKey,
                "series_rating_key": series_key,
                "season_number": season_number,
                "episode_number": item.index,
                "title": item.title,
                "file_path": self._mapped_path(item),
                "file_size_bytes": item.file_size,
                "added_at": epoch_to_iso(item.addedAt),
            }
        )

    def _store_user(self, account: PlexAccount) -> None:
        title = account.name or account.title or str(account.id)
        self._pending["users"].append(
            {
                "plex_user_id": str(account.id),
                "title": title,
                "is_owner": 1 if self.owner_title and title == self.owner_title else 0,
            }
        )

    def _store_play_event(self, entry: PlexMetadata) -> bool:
        if entry.accountID is None:
            return False
        user_id = self._user_ids.get(str(entry.accountID))
        if user_id is None:
            return False
        self._pending["play_events"].append(
            {
                "user_id": user_id,
                "plex_rating_key": entry.ratingKey,
                "watched": 1 if is_watched(entry.viewOffset, entry.duration) else 0,
                "played_at": epoch_to_iso(entry.viewedAt) or "",
                "position_ms": entry.viewOffset or 0,
                "duration_ms": entry.duration or 0,
                "source": "plex",
            }
        )
        return True

    def _mapped_path(self, item: PlexMetadata) -> str | None:
        path = item.file_path
        if not path:
            return None
        return self.client.map_path(path)
