"""Sync what Plex knows into the local database."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from almali_library_curator.plex.client import PlexClient
from almali_library_curator.plex.models import PlexAccount, PlexMetadata, is_watched

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    """How much a sync moved."""

    libraries: int = 0
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


class PlexSync:
    """Pulls the Plex library, the accounts and the watch history."""

    def __init__(
        self,
        client: PlexClient,
        conn: sqlite3.Connection,
        owner_title: str | None = None,
    ) -> None:
        self.client = client
        self.conn = conn
        self.owner_title = owner_title

    def run(self) -> SyncSummary:
        """Read everything from Plex and store it. Safe to run twice."""
        summary = SyncSummary()

        sections = self.client.sections()
        library_ids: dict[str, int] = {}
        for section in sections:
            library_ids[section.key] = self._store_library(section)
            summary.libraries += 1

        for section in sections:
            item_type = "movie" if (section.type or "").lower() == "movie" else "show"
            for item in self.client.section_items(section.key, item_type):
                stored_type = "movie" if item_type == "movie" else "series"
                self._store_media_item(item, library_ids[section.key], stored_type)
                summary.media_items += 1
                if item_type != "show":
                    continue
                for season_item in self.client.children(item.ratingKey, "season"):
                    self._store_media_item(
                        season_item,
                        library_ids[section.key],
                        "season",
                        parent_key=item.ratingKey,
                    )
                    summary.media_items += 1
                    for episode_item in self.client.children(season_item.ratingKey, "episode"):
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

        for entry in self.client.history():
            if self._store_play_event(entry):
                summary.play_events += 1

        self.conn.commit()
        logger.info("plex_sync_finished", extra={"summary": summary.__dict__})
        return summary

    # -- storage ---------------------------------------------------------

    def _upsert(
        self, table: str, key_columns: tuple[str, ...], values: dict[str, object]
    ) -> None:
        columns = list(values)
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
        self.conn.execute(statement, [values[column] for column in columns])

    def _store_library(self, section) -> int:
        self._upsert(
            "libraries",
            ("plex_key",),
            {
                "plex_key": section.key,
                "title": section.title,
                "section_type": section.type or "",
            },
        )
        row = self.conn.execute(
            "SELECT id FROM libraries WHERE plex_key = ?", (section.key,)
        ).fetchone()
        return int(row["id"])

    def _store_media_item(
        self,
        item: PlexMetadata,
        library_id: int,
        item_type: str,
        parent_key: str | None = None,
    ) -> None:
        self._upsert(
            "media_items",
            ("plex_rating_key",),
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
            },
        )

    def _store_episode(
        self,
        item: PlexMetadata,
        season_key: str,
        series_key: str,
        season_number: int | None,
    ) -> None:
        row = self.conn.execute(
            "SELECT id FROM media_items WHERE plex_rating_key = ?", (season_key,)
        ).fetchone()
        self._upsert(
            "episodes",
            ("plex_rating_key",),
            {
                "media_item_id": int(row["id"]) if row else None,
                "plex_rating_key": item.ratingKey,
                "series_rating_key": series_key,
                "season_number": season_number,
                "episode_number": item.index,
                "title": item.title,
                "file_path": self._mapped_path(item),
                "file_size_bytes": item.file_size,
                "added_at": epoch_to_iso(item.addedAt),
            },
        )

    def _store_user(self, account: PlexAccount) -> None:
        title = account.name or account.title or str(account.id)
        self._upsert(
            "users",
            ("plex_user_id",),
            {
                "plex_user_id": str(account.id),
                "title": title,
                "is_owner": 1 if self.owner_title and title == self.owner_title else 0,
            },
        )

    def _store_play_event(self, entry: PlexMetadata) -> bool:
        if entry.accountID is None:
            return False
        row = self.conn.execute(
            "SELECT id FROM users WHERE plex_user_id = ?", (str(entry.accountID),)
        ).fetchone()
        if row is None:
            return False
        self._upsert(
            "play_events",
            ("user_id", "plex_rating_key", "played_at", "position_ms"),
            {
                "user_id": int(row["id"]),
                "plex_rating_key": entry.ratingKey,
                "watched": 1 if is_watched(entry.viewOffset, entry.duration) else 0,
                "played_at": epoch_to_iso(entry.viewedAt) or "",
                "position_ms": entry.viewOffset or 0,
                "duration_ms": entry.duration or 0,
                "source": "plex",
            },
        )
        return True

    def _mapped_path(self, item: PlexMetadata) -> str | None:
        path = item.file_path
        if not path:
            return None
        return self.client.map_path(path)
