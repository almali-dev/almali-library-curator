"""Execution of requests passed by the final check (S5, S6, S15).

The curator never unlinks media itself. Each removal is routed by unit
type (S15 rule 1): movies go through Radarr and series or seasons through
Sonarr when that service is configured and its deletion switch is on, and
through Plex otherwise. A dry run sends no delete request at all and does
not perform even a collection cleanup.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from enum import StrEnum

from almali_library_curator.detection.models import Unit
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.lifecycle.engine import LifecycleEngine, _utc
from almali_library_curator.lifecycle.models import (
    DeletionOutcome, DeletionReport, DeletionStatus, RemovalRequest, RemovalState,
    RequestStateError,
)
from almali_library_curator.plex.client import (
    PlexAuthError, PlexClient, PlexUnavailableError,
)
from almali_library_curator.plex.deletion import PlexDeletionRefused, delete_media_item
from almali_library_curator.radarr import RadarrClient, RadarrNotFound
from almali_library_curator.sonarr import SonarrClient, SonarrNotFound

from almali_library_curator.lifecycle.safety import kill_switch_enabled

logger = logging.getLogger(__name__)


class FilePresence(StrEnum):
    GONE = "gone"
    PRESENT = "present"
    UNKNOWN = "unknown"


class VerificationError(RuntimeError):
    """A fixed diagnostic code, never an external exception body."""


# Fixed diagnostics stored as ``removal.failed.<code>`` (S16 rule 3).
FAILURE_CODES = frozenset({
    "empty_scope", "files_unknown", "files_present", "interrupted", "seasons_missing",
    "radarr_identity", "sonarr_identity", "plex_refused", "plex_auth", "plex_unavailable",
})

# Plex errors the owner can act on, most specific first (S15 rule 7).
_PLEX_DIAGNOSTICS = (
    (PlexDeletionRefused, "plex_refused"),
    (PlexAuthError, "plex_auth"),
    (PlexUnavailableError, "plex_unavailable"),
)


def _diagnostic(error: Exception) -> str:
    if isinstance(error, VerificationError):
        return str(error)
    for kind, code in _PLEX_DIAGNOSTICS:
        if isinstance(error, kind):
            return code
    return type(error).__name__


class DeletionExecutor:
    """Execute only ``recheck_passed`` requests using the engine's durable scope.

    The clock, settings and collection bookkeeping are shared with the T6
    engine. ``stat`` is an OS boundary, injectable for unreadable mounts.
    """

    def __init__(
        self, engine: LifecycleEngine, *, radarr: RadarrClient | None = None,
        sonarr: SonarrClient | None = None, plex: PlexClient | None = None,
        stat=os.stat, hostfs: HostFilesystem | None = None,
    ) -> None:
        self.engine = engine
        self.conn = engine.conn
        self.radarr = radarr
        self.sonarr = sonarr
        self.plex = plex
        self.stat = stat
        self.hostfs = (
            hostfs if hostfs is not None
            else HostFilesystem.from_settings(engine.settings)
        )

    def run_deletions(self, *, passed_ids=()) -> DeletionReport:
        """Only the caller's fresh final-check passes may be executed.

        Dry-run passes still have their original state because the check is
        non-persisting. An empty allowlist never authorizes a deletion.
        """
        outcomes = [] if self.engine.settings.dry_run else self._recover_interrupted()
        rows = [{"id": identifier} for identifier in sorted(set(passed_ids))]
        for row in rows:
            request_id = row["id"]
            try:
                request = self.engine._request_by_id(request_id)
            except Exception as error:
                # No readable scope means no safe external action. Preserve it
                # for repair, but never let it starve the remainder of the batch.
                status = DeletionStatus.FAILED
                diagnostic = type(error).__name__
                logger.warning(
                    "removal_request_unreadable",
                    extra={"request_id": request_id, "error": diagnostic},
                )
                outcomes.append(DeletionOutcome(request_id, status, error=diagnostic))
                continue
            allowed = (RemovalState.MARKED, RemovalState.REMOVAL_FAILED,
                       RemovalState.RECHECK_PASSED) if self.engine.settings.dry_run else (
                           RemovalState.RECHECK_PASSED,)
            if request.state not in allowed:
                continue  # The batch snapshot may have been cancelled meanwhile.
            if self.engine.settings.dry_run:
                try:
                    files = self._scope_files(request)
                except VerificationError as error:
                    # The preview reports the failure it would record, without
                    # mutating anything (S16 rule 2).
                    outcomes.append(DeletionOutcome(
                        request.id, DeletionStatus.FAILED, request,
                        error=f"removal.failed.{error}",
                    ))
                    continue
                snapshots = {}
                for path in files:
                    try:
                        snapshots[path] = self.stat(path)
                    except OSError:
                        pass
                size, _ = self._space(snapshots)
                outcomes.append(DeletionOutcome(
                    request.id, DeletionStatus.WOULD_DELETE, request,
                    size_bytes=size, files=tuple(files),
                ))
                continue
            if kill_switch_enabled(self.conn):
                outcomes.append(DeletionOutcome(
                    request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request,
                ))
                continue
            logger.info("removal_deletion_started", extra={"request_id": request.id})
            snapshots: dict[str, os.stat_result] = {}
            disks: set[str] = set()
            claimed = False
            try:
                files = self._scope_files(request)
                if not files:
                    raise VerificationError("empty_scope")
                for path in files:
                    disk = self.hostfs.disk_for(path)
                    if disk:
                        disks.add(disk)
                    try:
                        snapshots[path] = self.stat(path)
                    except OSError:
                        # Retries may find files already removed. Unknown facts
                        # must never contribute to a freed-space figure.
                        pass
                if not self._claim(request):
                    if kill_switch_enabled(self.conn):
                        outcomes.append(DeletionOutcome(
                            request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request,
                        ))
                    continue
                claimed = True
                if self._arr_responsible(request):
                    self._arr_delete(request)
                else:
                    self._plex_delete(request)
                presence = [self._presence(path) for path in files]
                if FilePresence.UNKNOWN in presence:
                    raise VerificationError("files_unknown")
                if FilePresence.PRESENT in presence:
                    raise VerificationError("files_present")
                size, freed = self._space(snapshots)
                outcomes.append(self._finish(
                    request, size, freed, ", ".join(sorted(disks)) or None,
                ))
            except Exception as error:
                # A preflight error still needs a durable failure, unless the
                # owner cancelled before execution could be claimed.
                if not claimed and not self._claim(request):
                    if kill_switch_enabled(self.conn):
                        outcomes.append(DeletionOutcome(
                            request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request,
                        ))
                    continue
                # Exception bodies can contain service URLs or credentials.
                # Persist only the error class, never arbitrary external text.
                diagnostic = _diagnostic(error)
                size, _ = self._space(snapshots)
                outcomes.append(self._fail(
                    request, diagnostic, size, ", ".join(sorted(disks)) or None,
                ))
        return DeletionReport(tuple(outcomes))

    def _claim(self, request: RemovalRequest) -> bool:
        """Seal cancellation durably before making any arr call.

        Serialize the switch read with the claim: a switch committed first
        always wins, even when it changes during file preflight.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        if kill_switch_enabled(self.conn):
            self.conn.commit()
            return False
        cursor = self.conn.execute(
            "UPDATE removal_requests SET state = ?, updated_at = ?"
            " WHERE id = ? AND state = ?",
            (RemovalState.DELETING.value, _utc(self.engine.clock()).isoformat(),
             request.id, RemovalState.RECHECK_PASSED.value),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def _recover_interrupted(self) -> list[DeletionOutcome]:
        """An interrupted claim must pass the final check again, never resume."""
        outcomes = []
        rows = self.conn.execute(
            "SELECT id FROM removal_requests WHERE state = ? ORDER BY id",
            (RemovalState.DELETING.value,),
        ).fetchall()
        for row in rows:
            try:
                request = self.engine._request_by_id(row["id"])
                outcomes.append(self._fail(request, "interrupted", 0, None))
            except Exception as error:
                self.conn.rollback()
                logger.warning(
                    "removal_recovery_failed",
                    extra={"request_id": row["id"], "error": type(error).__name__},
                )
        return outcomes

    @staticmethod
    def _space(snapshots: dict[str, os.stat_result]) -> tuple[int, int]:
        """All paths have been verified gone; count each inode at most once.

        Unknown pre-stats contribute nothing. An outside hardlink means the
        inode remains allocated even though every scope path disappeared.
        """
        inodes = defaultdict(list)
        for info in snapshots.values():
            inodes[(info.st_dev, info.st_ino)].append(info)
        size = sum(group[0].st_size for group in inodes.values())
        freed = sum(
            group[0].st_size for group in inodes.values()
            if all(info.st_nlink == len(group) for info in group)
        )
        return size, freed

    def _presence(self, path: str) -> FilePresence:
        try:
            self.stat(path)
        except FileNotFoundError:
            return FilePresence.GONE
        except OSError:
            return FilePresence.UNKNOWN
        return FilePresence.PRESENT

    def _scope_files(self, request: RemovalRequest) -> list[str]:
        """The container paths exactly the request's scope consists of.

        A seasons scope whose season rows are missing is its own failure
        (S16 rule 2): it is never treated as the series scope, because a
        series-scoped delete would remove more than the owner marked.
        """
        if request.unit is Unit.MOVIE:
            rows = self.conn.execute(
                "SELECT file_path FROM media_items WHERE id = ?", (request.media_item_id,)
            )
            files = {row["file_path"] for row in rows if row["file_path"]}
        else:
            rows = self.conn.execute(
                "SELECT file_path, season_number FROM episodes WHERE series_rating_key = ?",
                (self.engine._rating_key(request.media_item_id),),
            )
            if request.unit is Unit.SEASONS:
                wanted = set(request.seasons)
                scope_rows = [
                    row for row in rows if row["season_number"] in wanted
                ]
                # Every marked season must still have both its container
                # row and episode rows. A nonempty subset is not proof of
                # the whole scope: the backend could delete unverified files.
                season_rows = self.conn.execute(
                    "SELECT season_number FROM media_items"
                    " WHERE parent_rating_key = ? AND item_type = 'season'",
                    (self.engine._rating_key(request.media_item_id),),
                )
                known_seasons = {row["season_number"] for row in season_rows}
                episode_seasons = {row["season_number"] for row in scope_rows}
                if not wanted or not wanted <= known_seasons or episode_seasons != wanted:
                    raise VerificationError("seasons_missing")
                files = {
                    row["file_path"] for row in scope_rows if row["file_path"]
                }
            else:
                files = {row["file_path"] for row in rows if row["file_path"]}
        # PlexSync persists client.map_path(file_path), so mapping here again
        # would corrupt overlapping source/destination prefixes.
        return sorted(files)

    def _arr_responsible(self, request: RemovalRequest) -> bool:
        """S15 rules 1-2: a configured arr with its switch on owns its unit type.

        A configured and enabled arr wins even when it cannot handle the
        item: the removal then fails and retries, never falling back to
        Plex, because that arr could download the item again.
        """
        settings = self.engine.settings
        if request.unit is Unit.MOVIE:
            return self.radarr is not None and settings.radarr_deletes
        return self.sonarr is not None and settings.sonarr_deletes

    def _plex_keys(self, request: RemovalRequest) -> list[str]:
        """The Plex items whose deletion removes exactly the scope (S15 rule 4).

        A movie is the movie itself. A season or a series is one key per
        episode, so the series and season containers are never deleted.
        """
        if request.unit is Unit.MOVIE:
            rows = self.conn.execute(
                "SELECT plex_rating_key FROM media_items WHERE id = ?",
                (request.media_item_id,),
            )
            return [row["plex_rating_key"] for row in rows]
        rows = self.conn.execute(
            "SELECT plex_rating_key, season_number FROM episodes"
            " WHERE series_rating_key = ? ORDER BY season_number, episode_number",
            (self.engine._rating_key(request.media_item_id),),
        )
        wanted = set(request.seasons)
        return [
            row["plex_rating_key"] for row in rows
            if request.unit is Unit.SERIES or row["season_number"] in wanted
        ]

    def _plex_delete(self, request: RemovalRequest) -> None:
        if self.plex is None:
            raise RequestStateError("Plex is unavailable")
        keys = self._plex_keys(request)
        if not keys:
            raise VerificationError("empty_scope")
        for key in keys:
            # An item Plex no longer knows is not proof either: the files
            # are verified on disk afterwards, exactly like the arr path.
            delete_media_item(self.plex, key)

    def _arr_delete(self, request: RemovalRequest) -> None:
        item = self.conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        if request.unit is Unit.MOVIE:
            if self.radarr is None or item["radarr_id"] is None:
                raise VerificationError("radarr_identity")
            try:
                movie = self.radarr.unmonitor_movie(item["radarr_id"])
            except RadarrNotFound:
                return  # A retry still has to verify the files independently.
            if movie.tmdbId is None:
                raise RequestStateError("TMDB identity is unavailable")
            self.radarr.add_import_exclusion(movie.tmdbId, item["title"])
            try:
                self.radarr.delete_movie(item["radarr_id"], delete_files=True)
            except RadarrNotFound:
                pass
        else:
            if self.sonarr is None or item["sonarr_id"] is None:
                raise VerificationError("sonarr_identity")
            if request.unit is Unit.SEASONS:
                try:
                    self.sonarr.unmonitor_seasons(item["sonarr_id"], request.seasons)
                except SonarrNotFound:
                    return
                files = self.sonarr.episode_files(item["sonarr_id"])
                self.sonarr.delete_episode_files([
                    entry.id for entry in files
                    if entry.seasonNumber in request.seasons
                    and entry.seriesId == item["sonarr_id"]
                ])
            else:
                try:
                    series = self.sonarr.unmonitor_series(item["sonarr_id"])
                except SonarrNotFound:
                    return
                if series.tvdbId is None:
                    raise RequestStateError("TVDB identity is unavailable")
                self.sonarr.add_import_exclusion(series.tvdbId, item["title"])
                try:
                    self.sonarr.delete_series(item["sonarr_id"], delete_files=True)
                except SonarrNotFound:
                    pass

    def _finish(
        self, request: RemovalRequest, size: int, freed: int, disk: str | None,
    ) -> DeletionOutcome:
        now = _utc(self.engine.clock()).isoformat()
        cursor = self.conn.execute(
            "UPDATE removal_requests SET state = ?, deleted_at = ?, last_error = NULL,"
            " updated_at = ? WHERE id = ? AND state = ?",
            (RemovalState.DELETED.value, now, now, request.id, RemovalState.DELETING.value),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise RequestStateError("Request changed during deletion")
        self._record_event(request, "deleted", now, size, freed, disk)
        self.conn.commit()
        self.engine.retry_collection_cleanup()
        logger.info("removal_deleted", extra={"request_id": request.id, "freed_bytes": freed})
        return DeletionOutcome(request.id, DeletionStatus.DELETED,
                               self.engine._request_by_id(request.id), size, freed)

    def _record_event(
        self, request: RemovalRequest, outcome: str, now: str, size: int,
        freed: int, disk: str | None, error: str | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT reason FROM removal_requests WHERE id = ?", (request.id,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO removal_events (removal_request_id, media_item_id, outcome,"
            " title, unit, category, reason, size_bytes, freed_bytes, disk, marked_by,"
            " error, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request.id, request.media_item_id, outcome, self.engine._history_title(request),
             request.unit.value, request.category.value, row["reason"], size, freed, disk,
             request.marked_by, error, now),
        )

    def _fail(
        self, request: RemovalRequest, diagnostic: str, size: int, disk: str | None,
    ) -> DeletionOutcome:
        """Record the failure as a translation key, never rendered text.

        S16 rule 3: the stored value keeps the ``removal.failed.*`` key so
        the screen translates it in the reader's language and a locale
        change never rewrites history. The raw diagnostic stays in the
        log only.
        """
        key = diagnostic if diagnostic in FAILURE_CODES else "service"
        error = f"removal.failed.{key}"
        now = _utc(self.engine.clock()).isoformat()
        self.conn.rollback()
        cursor = self.conn.execute(
            "UPDATE removal_requests SET state = ?, last_error = ?, updated_at = ?"
            " WHERE id = ? AND state = ?",
            (RemovalState.REMOVAL_FAILED.value, error, now, request.id, RemovalState.DELETING.value),
        )
        if cursor.rowcount == 1:
            self._record_event(request, "failed", now, size, 0, disk, error)
        self.conn.commit()
        logger.warning("removal_deletion_failed", extra={"request_id": request.id, "error": diagnostic})
        return DeletionOutcome(request.id, DeletionStatus.FAILED,
                               self.engine._request_by_id(request.id), error=error)
