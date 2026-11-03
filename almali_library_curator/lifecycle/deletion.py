"""Sonarr/Radarr execution of requests passed by the final check (S5–S6).

The curator never unlinks media itself. All destructive writes go through
arr; a dry run does not perform even a collection cleanup.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from enum import StrEnum

from almali_library_curator.detection import text
from almali_library_curator.detection.models import Unit
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.lifecycle.engine import LifecycleEngine, _utc
from almali_library_curator.lifecycle.models import (
    DeletionOutcome, DeletionReport, DeletionStatus, RemovalRequest, RemovalState,
    RequestStateError,
)
from almali_library_curator.radarr import RadarrClient, RadarrNotFound
from almali_library_curator.sonarr import SonarrClient, SonarrNotFound

logger = logging.getLogger(__name__)


class FilePresence(StrEnum):
    GONE = "gone"
    PRESENT = "present"
    UNKNOWN = "unknown"


class VerificationError(RuntimeError):
    """A fixed diagnostic code, never an external exception body."""


class DeletionExecutor:
    """Execute only ``recheck_passed`` requests using the engine's durable scope.

    The clock, settings and collection bookkeeping are shared with the T6
    engine. ``stat`` is an OS boundary, injectable for unreadable mounts.
    """

    def __init__(
        self, engine: LifecycleEngine, *, radarr: RadarrClient | None = None,
        sonarr: SonarrClient | None = None, stat=os.stat,
        hostfs: HostFilesystem | None = None,
    ) -> None:
        self.engine = engine
        self.conn = engine.conn
        self.radarr = radarr
        self.sonarr = sonarr
        self.stat = stat
        self.hostfs = (
            hostfs if hostfs is not None
            else HostFilesystem.from_settings(engine.settings)
        )

    def run_deletions(self) -> DeletionReport:
        outcomes = [] if self.engine.settings.dry_run else self._recover_interrupted()
        rows = self.conn.execute(
            "SELECT id FROM removal_requests WHERE state = ? ORDER BY id",
            (RemovalState.RECHECK_PASSED.value,),
        ).fetchall()
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
            if request.state is not RemovalState.RECHECK_PASSED:
                continue  # The batch snapshot may have been cancelled meanwhile.
            if self.engine.settings.dry_run:
                outcomes.append(DeletionOutcome(
                    request.id, DeletionStatus.WOULD_DELETE, request,
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
                    continue
                claimed = True
                self._arr_delete(request)
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
                    continue
                # Exception bodies can contain service URLs or credentials.
                # Persist only the error class, never arbitrary external text.
                diagnostic = (
                    str(error) if isinstance(error, VerificationError)
                    else type(error).__name__
                )
                size, _ = self._space(snapshots)
                outcomes.append(self._fail(
                    request, diagnostic, size, ", ".join(sorted(disks)) or None,
                ))
        return DeletionReport(tuple(outcomes))

    def _claim(self, request: RemovalRequest) -> bool:
        """Seal cancellation durably before making any arr call."""
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
        if request.unit is Unit.MOVIE:
            rows = self.conn.execute(
                "SELECT file_path FROM media_items WHERE id = ?", (request.media_item_id,)
            )
        else:
            rows = self.conn.execute(
                "SELECT file_path, season_number FROM episodes WHERE series_rating_key = ?",
                (self.engine._rating_key(request.media_item_id),),
            )
        # PlexSync persists client.map_path(file_path), so mapping here again
        # would corrupt overlapping source/destination prefixes.
        return sorted({row["file_path"] for row in rows if row["file_path"] and (
            request.unit is not Unit.SEASONS or row["season_number"] in request.seasons
        )})

    def _arr_delete(self, request: RemovalRequest) -> None:
        item = self.conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        if request.unit is Unit.MOVIE:
            if self.radarr is None or item["radarr_id"] is None:
                raise RequestStateError("Radarr identity is unavailable")
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
                raise RequestStateError("Sonarr identity is unavailable")
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
        key = (
            diagnostic if diagnostic in {
                "empty_scope", "files_unknown", "files_present", "interrupted",
            }
            else "service"
        )
        error = text.render(self.engine.locale, f"removal.failed.{key}", error=diagnostic)
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
