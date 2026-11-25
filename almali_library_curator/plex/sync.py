"""Sync Plex snapshots without deleting stored items or their history.

Every remote read is consumed one page at a time and the rows of a page are
stored as it arrives, so a library of any size is never held whole in memory
(#60). The snapshot is still published in one write transaction that opens
after the last page, so a read that pages slowly never holds SQLite's writer
slot and owner controls can commit while Plex is being fetched (#87, #90).
The rows of the pages that arrive before that transaction opens wait in a
private spool file, never in memory, and are read back one page at a time
when the snapshot is published.

The sync owns that write transaction: a connection that already has an open
transaction keeps it, and the sync joins it with a savepoint instead of
committing or rolling back the caller's pending work. Only complete section
reads establish absence. Absent rows remain stored with
``plex_present = 0`` and leave detection and removal scope; a later
observation restores the same row. Failed reads roll back, and incomplete TV
hierarchies upsert observations without pruning anything. Unread sections
are unchanged.

The watch history is read whole on every sync (#112): a play that Plex
stores late with the instant it happened at is older than the newest play
already stored, so a read that started from that one would never see it.
The unique index on the play events and the unchanged-row check below keep
reading the same history again from storing anything twice.

A caller may hand ``on_publish`` a callback that runs inside that publish
transaction, after the local writes and before the commit (#36). Its writes
commit or roll back with the snapshot they describe, and the writer slot is
never held across the remote reads above the boundary.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from typing import TextIO

from almali_library_curator import library_scope
from almali_library_curator.plex.client import PlexClient, PlexNotFoundError
from almali_library_curator.plex.models import PlexAccount, PlexMetadata, is_watched

logger = logging.getLogger(__name__)

# A column of the wall clock alone never makes a stored row dirty (#74):
# writing it is not a change a reader can see.
_VOLATILE_COLUMNS = ("updated_at",)


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


def history_entry_is_watched(entry: PlexMetadata) -> bool:
    """A Plex history entry counts as watched even without a position.

    The history endpoint reports what was watched, but a live server often
    omits ``viewOffset`` and ``duration``. When Plex reports both, the 90%
    rule refines the answer; otherwise the entry itself is the evidence.
    """
    if entry.viewOffset is None or entry.duration is None:
        return True
    return is_watched(entry.viewOffset, entry.duration)


class _RowSpool:
    """The rows of one sync, written to disk page by page (#60).

    The snapshot is published in one transaction that opens after every
    remote read has finished, so a page cannot be written to the database on
    arrival (#87, #90). Holding the rows in memory until then is what #60
    measures in hundreds of megabytes, so each arriving page is written to a
    private temporary file and read back one page at a time: at most one page
    of rows is ever live in memory. Row values are what the tables store
    (text, integers and nulls), so a page survives the round trip unchanged.
    """

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="alc-plex-sync-")
        self._writers: dict[str, TextIO] = {}

    def append(self, table: str, rows: Sequence[dict[str, object]]) -> None:
        """Write one page of rows as it arrives."""
        if not rows:
            return
        writer = self._writers.get(table)
        if writer is None:
            path = Path(self._directory.name) / f"{table}.jsonl"
            writer = path.open("w", encoding="utf-8")
            self._writers[table] = writer
        writer.write(json.dumps(rows, separators=(",", ":")) + "\n")

    def pages(self, table: str) -> Iterator[list[dict[str, object]]]:
        """The pages of one table, in the order they arrived."""
        path = Path(self._directory.name) / f"{table}.jsonl"
        writer = self._writers.pop(table, None)
        if writer is not None:
            writer.close()
        if not path.exists():
            return
        with path.open(encoding="utf-8") as reader:
            for line in reader:
                yield json.loads(line)

    def close(self) -> None:
        """Drop the spooled rows, whether they were published or not."""
        for writer in self._writers.values():
            try:
                writer.close()
            except OSError:
                pass
        self._writers.clear()
        try:
            self._directory.cleanup()
        except OSError:
            pass


@dataclass
class _SectionRead:
    """What one section's read proved, gathered page by page (#60).

    The rows of a page go to the spool as they arrive; what stays here is
    only what the completeness proof and the pruning rule need, never a row:
    the rating keys already seen and the counts each parent reports.
    """

    item_type: str
    # Series rating key -> the counts the series reports for its children.
    series: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    series_order: list[str] = field(default_factory=list)
    # Season rating key -> (series key, season number, reported leaf count).
    seasons: dict[str, tuple[str | None, int | None, int | None]] = field(
        default_factory=dict
    )
    season_order: list[str] = field(default_factory=list)
    stored_seasons: set[str] = field(default_factory=set)
    seasons_per_series: dict[str, int] = field(default_factory=dict)
    episodes_per_series: dict[str, int] = field(default_factory=dict)
    episodes_per_season: dict[str, int] = field(default_factory=dict)
    episodes: set[str] = field(default_factory=set)
    complete: bool = True

    @property
    def is_show(self) -> bool:
        return self.item_type != "movie"

    def add_series_or_movie(self, item: PlexMetadata) -> None:
        """Record one item of the section."""
        if not self.is_show:
            return
        if item.ratingKey in self.series:
            self.complete = False
            return
        self.series_order.append(item.ratingKey)
        self.series[item.ratingKey] = (item.childCount, item.leafCount)

    def add_season(self, item: PlexMetadata) -> bool:
        """Record one season; True when its series is in this section."""
        if item.ratingKey in self.seasons:
            self.complete = False
        else:
            self.season_order.append(item.ratingKey)
        self.seasons[item.ratingKey] = (
            item.parentRatingKey, item.index, item.leafCount,
        )
        parent = item.parentRatingKey
        if parent is None or parent not in self.series:
            # A season of a series the section did not list is an orphan.
            self.complete = False
            return False
        self.seasons_per_series[parent] = self.seasons_per_series.get(parent, 0) + 1
        self.stored_seasons.add(item.ratingKey)
        return True

    def add_episode(self, item: PlexMetadata) -> bool:
        """Record one episode; True when its season is in this section."""
        if item.ratingKey in self.episodes:
            self.complete = False
        self.episodes.add(item.ratingKey)
        parent = item.parentRatingKey or ""
        season = self.seasons.get(parent)
        if season is None:
            self.complete = False
            return False
        series_key, _season_number, _leaf_count = season
        if series_key is None or item.grandparentRatingKey not in (None, series_key):
            self.complete = False
            return False
        self.episodes_per_season[parent] = self.episodes_per_season.get(parent, 0) + 1
        self.episodes_per_series[series_key] = (
            self.episodes_per_series.get(series_key, 0) + 1
        )
        return parent in self.stored_seasons

    def proves_complete(self) -> bool:
        """HTTP success alone is not proof of a complete TV hierarchy.

        Missing counts cannot prove completeness. A mismatch, orphan or
        duplicate leaves the entire section's previous presence intact.
        """
        if not self.is_show:
            return True
        return (
            self.complete
            and all(
                child_count is not None
                and child_count == self.seasons_per_series.get(key, 0)
                and leaf_count is not None
                and leaf_count == self.episodes_per_series.get(key, 0)
                for key, (child_count, leaf_count) in self.series.items()
            )
            and all(
                leaf_count is not None
                and leaf_count == self.episodes_per_season.get(key, 0)
                for key, (_parent, _index, leaf_count) in self.seasons.items()
            )
        )


class PlexSync:
    """Pulls the Plex library, the accounts and the watch history."""

    def __init__(
        self,
        client: PlexClient,
        conn: sqlite3.Connection,
        owner_title: str | None = None,
        curated_libraries: tuple[str, ...] = (),
        on_publish: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.client = client
        self.conn = conn
        self.owner_title = owner_title
        # Runs inside the publish transaction, after the local writes and
        # before the commit (#36). A hook that raises rolls the whole
        # snapshot back, so it never commits without its own additions.
        self.on_publish = on_publish
        # S14: an empty selection means every library is curated.
        self.curated_libraries = tuple(curated_libraries)
        # S24: the rows of a table are written in one batch, instead of one
        # statement per row. Only the two small tables read in one request
        # stay in memory; every other row waits in the spool below.
        self._pending: dict[str, list[dict[str, object]]] = {
            "libraries": [],
            "users": [],
        }
        # The rows of the pages that arrive before the publication opens.
        self._spool = _RowSpool()
        # The rating keys the publication wrote, so only the rest of a
        # complete section is marked absent (#111).
        self._written: dict[str, set[str]] = {"media_items": set(), "episodes": set()}
        # What each section's read proved, and the series keys it listed.
        self._reads: list[tuple[str, _SectionRead]] = []
        # The id of every user, read once after the users are written.
        self._user_ids: dict[str, int] = {}
        # The id of every stored season, read once the items are written.
        self._season_ids: dict[str, int] | None = None
        # How the snapshot is published: our own write transaction, or a
        # savepoint inside the transaction the caller already opened.
        self._mode = 'own'

    def run(self) -> SyncSummary:
        """Read everything from Plex and store it. Safe to run twice.

        Every Plex section gets a ``libraries`` row, because that list is
        what the owner picks the curated scope from. Items, seasons and
        episodes are fetched only for the curated libraries, so an
        uncurated library produces no outbound request at all (S14 rule 2).

        The snapshot is published in a write transaction. A connection that
        already has an open transaction keeps it: the sync joins it with a
        savepoint and never commits or rolls back the caller's pending
        work. The caller owns that transaction and commits it.
        """
        # A reused sync must not replay buffered rows from a failed attempt.
        self._discard()
        self._spool = _RowSpool()
        # An open transaction is the caller's: it is joined, never closed.
        self._mode = 'savepoint' if self.conn.in_transaction else 'own'
        try:
            return self._run()
        except Exception:
            self._abort()
            raise
        finally:
            self._discard()

    def _discard(self) -> None:
        """Drop everything one attempt gathered, in memory and on disk."""
        for rows in self._pending.values():
            rows.clear()
        self._reads.clear()
        self._season_ids = None
        self._spool.close()

    def _begin(self) -> None:
        """Open the write transaction the snapshot is published in."""
        if self._mode == 'savepoint':
            self.conn.execute("SAVEPOINT plex_sync")
        else:
            self.conn.execute("BEGIN IMMEDIATE")

    def _publish(self) -> None:
        """Publish the snapshot, keeping the caller's transaction open."""
        if self._mode == 'savepoint':
            self.conn.execute("RELEASE SAVEPOINT plex_sync")
        else:
            self.conn.commit()

    def _abort(self) -> None:
        """Undo the sync's own work, never the caller's pending work."""
        if self._mode == 'savepoint':
            try:
                self.conn.execute("ROLLBACK TO SAVEPOINT plex_sync")
                self.conn.execute("RELEASE SAVEPOINT plex_sync")
            except sqlite3.Error:
                pass  # the savepoint never opened: the caller's work stays
        else:
            self.conn.rollback()

    def _run(self) -> SyncSummary:
        summary = SyncSummary()
        complete_libraries: list[int] = []
        sections = self.client.sections()
        for section in sections:
            self._store_library(section)
            summary.libraries += 1
            if library_scope.library_is_curated(section.key, self.curated_libraries):
                summary.curated_libraries += 1
        # Every page is stored as it arrives: the sync holds one page of
        # models and one page of rows, whatever the size of the library.
        for section in sections:
            if not library_scope.library_is_curated(section.key, self.curated_libraries):
                continue
            self._read_section(section, summary)
        for account in self.client.accounts():
            self._store_user(account)
            summary.users += 1
        self._read_history()

        # No HTTP calls below this boundary. Resolve local ids under the same
        # short transaction that publishes upserts and presence changes.
        self._begin()
        library_ids = self._write_libraries()
        for section_key, read in self._reads:
            if read.proves_complete():
                complete_libraries.append(library_ids[section_key])
            else:
                logger.warning("plex_section_incomplete_no_pruning", extra={"section": section_key})
        # Only fully read sections establish absence. This and the upserts
        # commit together: a failed sync never publishes a partial snapshot.
        # A show may move between sections. Its old section's absence
        # cannot prune children the incomplete destination did not return.
        uncertain_series = json.dumps(sorted({
            series_key
            for section_key, read in self._reads
            if library_ids[section_key] not in complete_libraries
            for series_key in read.series
        }))
        # Upsert first, then mark absent only what this sync did not write
        # (#111): marking a whole complete section absent and restoring it
        # rewrote every row twice. A row that is already absent stays as it
        # is. The upserts also move an item to the library it now belongs
        # to, so a moved item is written, never marked absent.
        self._written = {"media_items": set(), "episodes": set()}
        self._write_media_items(library_ids)
        self._write_episodes()
        written_items = json.dumps(sorted(self._written["media_items"]))
        written_episodes = json.dumps(sorted(self._written["episodes"]))
        self._written = {"media_items": set(), "episodes": set()}
        for library_id in complete_libraries:
            self.conn.execute(
                "UPDATE episodes SET plex_present = 0 WHERE plex_present != 0"
                " AND series_rating_key IN"
                " (SELECT plex_rating_key FROM media_items WHERE library_id = ?)"
                " AND series_rating_key NOT IN (SELECT value FROM json_each(?))"
                " AND plex_rating_key NOT IN (SELECT value FROM json_each(?))",
                (library_id, uncertain_series, written_episodes),
            )
            self.conn.execute(
                "UPDATE media_items SET plex_present = 0 WHERE plex_present != 0"
                " AND library_id = ?"
                " AND (parent_rating_key IS NULL OR parent_rating_key NOT IN"
                " (SELECT value FROM json_each(?)))"
                " AND plex_rating_key NOT IN (SELECT value FROM json_each(?))",
                (library_id, uncertain_series, written_items),
            )
        self._write_users()
        summary.play_events = self._write_play_events()
        if self.on_publish is not None:
            # Still inside the transaction opened above: whatever the hook
            # writes belongs to this snapshot, not to a later commit.
            self.on_publish(self.conn)
        self._publish()
        # Which publishing mode this snapshot used: a savepoint inside the
        # caller's transaction defers it until the caller commits, so a
        # finished line without a persisted snapshot is diagnosable here.
        logger.info(
            "plex_sync_finished",
            extra={"summary": summary.__dict__, "publishing": self._mode},
        )
        return summary

    # -- reading ---------------------------------------------------------

    def _read_section(self, section, summary: SyncSummary) -> None:
        """Read one curated section, storing the rows of each page."""
        item_type = "movie" if (section.type or "").lower() == "movie" else "show"
        stored_type = "movie" if item_type == "movie" else "series"
        read = _SectionRead(item_type)

        def store(page: list[PlexMetadata]) -> None:
            rows = []
            for item in page:
                read.add_series_or_movie(item)
                rows.append(self._media_item_row(item, section.key, stored_type))
            self._spool.append("media_items", rows)
            summary.media_items += len(rows)

        self.client.section_items(section.key, item_type, on_page=store)
        if read.is_show:
            # S23: the seasons and the episodes of the whole section are
            # read at once instead of one request per series and per
            # season. The rows stored are the same rows.
            self._seasons_of(section.key, read, summary)
            self._episodes_of(section.key, read, summary)
        self._reads.append((section.key, read))

    def _seasons_of(
        self, section_key: str, read: _SectionRead, summary: SyncSummary
    ) -> None:
        """The seasons of a show section, read in one request.

        A server that does not know the bulk read (HTTP 404), or that
        answers it with nothing although the series report seasons, is read
        the old way, one series at a time, so the sync stores the same rows.
        """
        answered: set[str] = set()

        def store(page: list[PlexMetadata]) -> None:
            rows = []
            for season in page:
                answered.add(season.parentRatingKey or "")
                if read.add_season(season):
                    rows.append(self._media_item_row(
                        season, section_key, "season",
                        parent_key=season.parentRatingKey,
                    ))
            self._spool.append("media_items", rows)
            summary.media_items += len(rows)

        try:
            self.client.section_items(
                section_key, "season", include_guids=False, on_page=store
            )
        except PlexNotFoundError:
            logger.warning(
                "plex_bulk_seasons_unavailable", extra={"section": section_key}
            )
        missing = [
            series_key
            for series_key in read.series_order
            if read.series[series_key][0] and series_key not in answered
        ]
        if missing:
            logger.warning(
                "plex_bulk_seasons_incomplete", extra={"section": section_key}
            )
            for series_key in missing:
                self.client.children(series_key, "season", on_page=store)

    def _episodes_of(
        self, section_key: str, read: _SectionRead, summary: SyncSummary
    ) -> None:
        """The episodes of a show section, read in one request.

        A server that does not know the bulk read (HTTP 404), or that
        answers it with nothing although the seasons report episodes, is
        read the old way, one season at a time.
        """
        answered: set[str] = set()

        def store(page: list[PlexMetadata]) -> None:
            rows = []
            for episode in page:
                answered.add(episode.parentRatingKey or "")
                if not read.add_episode(episode):
                    continue
                series_key, season_number, _leaf_count = read.seasons[
                    episode.parentRatingKey or ""
                ]
                rows.append(self._episode_row(
                    episode, episode.parentRatingKey or "", series_key, season_number
                ))
            self._spool.append("episodes", rows)
            summary.episodes += len(rows)

        try:
            self.client.section_items(
                section_key, "episode", include_guids=False, on_page=store
            )
        except PlexNotFoundError:
            logger.warning(
                "plex_bulk_episodes_unavailable", extra={"section": section_key}
            )
        missing = [
            season_key
            for season_key in read.season_order
            if read.seasons[season_key][2] and season_key not in answered
        ]
        if missing:
            logger.warning(
                "plex_bulk_episodes_incomplete", extra={"section": section_key}
            )
            for season_key in missing:
                self.client.children(season_key, "episode", on_page=store)

    def _read_history(self) -> None:
        """The whole watch history, stored page by page as it arrives.

        Every page is read on every sync (#112). Plex can store a play late
        with the instant it happened at, so it is older than the newest play
        already stored; a read that started from that one would skip it for
        good. The unique index on the play events and the unchanged-row check
        of the upsert keep a second read of the same history from storing
        anything twice.
        """

        def store(page: list[PlexMetadata]) -> None:
            rows = []
            for entry in page:
                row = self._play_event_row(entry)
                if row is not None:
                    rows.append(row)
            self._spool.append("play_events", rows)

        self.client.history(on_page=store)

    # -- storage ---------------------------------------------------------

    def _upsert_many(
        self, table: str, key_columns: tuple[str, ...], rows: Iterable[dict[str, object]]
    ) -> None:
        """Write many rows of one table in one batch (S24).

        The rows of a table always carry the same columns, so the statement
        is built once and every row is written through it. The rows may be a
        generator that reads them back from the spool a page at a time, so
        one statement writes the table without holding it in memory (#60).
        """
        pending = iter(rows)
        first = next(pending, None)
        if first is None:
            return
        columns = list(first)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in key_columns
        )
        # A row whose stored values did not change is not rewritten (#74):
        # the wall-clock stamp alone never makes it dirty, so a second sync
        # of an unchanged library writes nothing at all.
        compared = [
            column for column in columns
            if column not in key_columns and column not in _VOLATILE_COLUMNS
        ]
        changed = " OR ".join(
            f"{table}.{column} IS NOT excluded.{column}" for column in compared
        )
        conflict = ", ".join(key_columns)
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
            + (f" WHERE {changed}" if changed else "")
        )
        self.conn.executemany(
            statement,
            (
                [row[column] for column in columns]
                for row in chain([first], pending)
            ),
        )

    def _write_libraries(self) -> dict[str, int]:
        """Write the libraries and read their ids in one go."""
        self._upsert_many("libraries", ("plex_key",), self._pending["libraries"])
        self._pending["libraries"] = []
        return {
            row["plex_key"]: int(row["id"])
            for row in self.conn.execute("SELECT id, plex_key FROM libraries")
        }

    def _write_media_items(self, library_ids: dict[str, int]) -> None:
        """Write the media items, reading the spool one page at a time."""
        self._upsert_many(
            "media_items", ("plex_rating_key",), self._media_item_rows(library_ids)
        )

    def _media_item_rows(
        self, library_ids: dict[str, int]
    ) -> Iterator[dict[str, object]]:
        for page in self._spool.pages("media_items"):
            for pending in page:
                row = dict(pending)
                row["library_id"] = library_ids[row.pop("_library_key")]
                self._written["media_items"].add(row["plex_rating_key"])
                yield row

    def _write_episodes(self) -> None:
        """Write the episodes a page at a time, with the seasons read once."""
        self._upsert_many("episodes", ("plex_rating_key",), self._episode_rows())

    def _episode_rows(self) -> Iterator[dict[str, object]]:
        for page in self._spool.pages("episodes"):
            season_ids = self._season_ids_by_key()
            for pending in page:
                row = dict(pending)
                row["media_item_id"] = season_ids.get(row.pop("_season_key"))
                self._written["episodes"].add(row["plex_rating_key"])
                yield row

    def _season_ids_by_key(self) -> dict[str, int]:
        """The id of every stored season, read once the items are written."""
        if self._season_ids is None:
            self._season_ids = {
                row["plex_rating_key"]: int(row["id"])
                for row in self.conn.execute(
                    "SELECT id, plex_rating_key FROM media_items"
                    " WHERE item_type = 'season'"
                )
            }
        return self._season_ids

    def _write_users(self) -> None:
        """Write the users and read their ids in one go."""
        self._upsert_many("users", ("plex_user_id",), self._pending["users"])
        self._pending["users"] = []
        self._user_ids = {
            row["plex_user_id"]: int(row["id"])
            for row in self.conn.execute("SELECT id, plex_user_id FROM users")
        }

    def _write_play_events(self) -> int:
        """Write the play events a page at a time, resolving their user."""
        written = 0

        def rows() -> Iterator[dict[str, object]]:
            nonlocal written
            for page in self._spool.pages("play_events"):
                for pending in page:
                    row = dict(pending)
                    user_id = self._user_ids.get(str(row.pop("_plex_user_id")))
                    if user_id is None:
                        continue
                    row["user_id"] = user_id
                    written += 1
                    yield row

        self._upsert_many(
            "play_events",
            ("user_id", "plex_rating_key", "played_at", "position_ms"),
            rows(),
        )
        return written

    def _store_library(self, section) -> None:
        self._pending["libraries"].append(
            {
                "plex_key": section.key,
                "title": section.title,
                "section_type": section.type or "",
            }
        )

    def _media_item_row(
        self,
        item: PlexMetadata,
        library_key: str,
        item_type: str,
        parent_key: str | None = None,
    ) -> dict[str, object]:
        return {
            # The library is resolved when the page is written, once the
            # section keys of this run are stored.
            "library_id": None,
            "plex_present": 1,
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
            "_library_key": library_key,
        }

    def _episode_row(
        self,
        item: PlexMetadata,
        season_key: str,
        series_key: str,
        season_number: int | None,
    ) -> dict[str, object]:
        return {
            # The season of the episode is resolved when the page is
            # written, once the seasons are stored.
            "_season_key": season_key,
            "media_item_id": None,
            "plex_present": 1,
            "plex_rating_key": item.ratingKey,
            "series_rating_key": series_key,
            "season_number": season_number,
            "episode_number": item.index,
            "title": item.title,
            "file_path": self._mapped_path(item),
            "file_size_bytes": item.file_size,
            "added_at": epoch_to_iso(item.addedAt),
        }

    def _store_user(self, account: PlexAccount) -> None:
        title = account.name or account.title or str(account.id)
        self._pending["users"].append(
            {
                "plex_user_id": str(account.id),
                "title": title,
                "is_owner": 1 if self.owner_title and title == self.owner_title else 0,
            }
        )

    def _play_event_row(self, entry: PlexMetadata) -> dict[str, object] | None:
        """One play event, carrying the account it belongs to.

        The user id is resolved when the page is written: the users are
        stored after every page of history has arrived. An entry that no
        longer links any item (#118, a removed item's play) is skipped.
        """
        if entry.accountID is None or entry.ratingKey is None:
            return None
        return {
            "user_id": None,
            "plex_rating_key": entry.ratingKey,
            "watched": 1 if history_entry_is_watched(entry) else 0,
            "played_at": epoch_to_iso(entry.viewedAt) or "",
            "position_ms": entry.viewOffset or 0,
            "duration_ms": entry.duration or 0,
            "source": "plex",
            "_plex_user_id": str(entry.accountID),
        }

    def _mapped_path(self, item: PlexMetadata) -> str | None:
        path = item.file_path
        if not path:
            return None
        return self.client.map_path(path)
