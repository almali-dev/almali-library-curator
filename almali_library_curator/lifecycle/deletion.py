"""Execution of requests passed by the final check (S5, S6, S15).

The curator never unlinks media itself. Each removal is routed by unit
type (S15 rule 1): movies go through Radarr and series or seasons through
Sonarr when that service is configured and its deletion switch is on, and
through Plex otherwise. A dry run sends no delete request at all and does
not perform even a collection cleanup.

The "deleted" claim is strict per file (#39, parent decision 2026-11-16):
every file of the marked scope must have been observed on disk now or by
an earlier attempt, and confirmed gone after the call. A scope holding a
file that was never visible fails with ``files_unknown`` and sends
nothing, even when a sibling file was observed: that means the environment
is not what the app believes (an offline array disk, a wrong path map),
and this tool refuses to act on facts it cannot see. The owner fixes the
environment and re-marks; removal events that recorded real removals stay
in the history, but the request's outcome is the failure.

Known limits of the ownership proof (#38), accepted with the current data:

* The path comparison is byte-exact and case-sensitive, which is correct
  on the Linux hosts the app runs on.
* A movie's ownership is proven against its folder, because the Radarr
  model carries no per-file path.
* A series scope deletes the extra files the series folder holds by
  design: deleting the series is the only Sonarr operation that removes
  the resource itself.
* The season equality rule has no legitimate exit: one extra episode file
  in a marked season blocks that season, because deleting it would remove
  files the owner never marked. A Sonarr payload whose episode files carry
  no ``path`` also fails closed forever, as an API shape mismatch.
* SECURITY.md still states only "files are confirmed gone from disk"; the
  fuller "seen before the call, or seen by an earlier attempt" wording is
  the parent's to update, not this module's.
"""

from __future__ import annotations

import logging
import os
import sqlite3
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
from almali_library_curator.plex.deletion import (
    PlexDeletionRefused, PlexMultiVersionError, delete_media_item,
)
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
# ``radarr_identity``/``sonarr_identity`` cover every ownership refusal of
# #38: no stored arr id, a missing resource on the re-GET, an external id
# that does not match the Plex item, a resource path that does not own
# every marked file after the path map, an arr that holds fewer files than
# the marked scope, or a Sonarr payload whose episode files carry no
# ``path``. ``files_unknown`` covers every refusal of #39 to claim
# "deleted" without having seen the files: an unreadable media root, a
# failed pre-delete stat, a scope no attempt ever saw, and a scope holding
# a file that was never visible; the wording names the remedy (fix the
# environment and mark the item again).
FAILURE_CODES = frozenset({
    "empty_scope", "files_unknown", "files_present", "interrupted", "seasons_missing",
    "radarr_identity", "sonarr_identity", "plex_refused", "plex_multi_version",
    "plex_auth", "plex_unavailable",
})

# Plex errors the owner can act on, most specific first (S15 rule 7).
_PLEX_DIAGNOSTICS = (
    (PlexDeletionRefused, "plex_refused"),
    (PlexMultiVersionError, "plex_multi_version"),
    (PlexAuthError, "plex_auth"),
    (PlexUnavailableError, "plex_unavailable"),
)


def _mapped_host_path(path: str | None, settings) -> str | None:
    """An arr (host-side) path mapped into the app's media namespace.

    The same Plex path map that translates Plex's host paths into the
    container's ``/media`` view applies to the arr resource paths: both
    live on the host, while ``media_items.file_path`` is already mapped.
    """
    if not path:
        return None
    for source, target in settings.plex_path_map:
        if path == source:
            return target
        prefix = source.rstrip("/")
        if path.startswith(prefix + "/"):
            return target.rstrip("/") + path[len(prefix):]
    return path


def _under_resource(path: str | None, root: str | None, settings) -> bool:
    """Whether ``path`` (already mapped) lies under the resource path.

    An empty or root (``/``) resource path owns nothing: mapping it to
    "every absolute path" would fail the ownership proof open (#38).
    """
    mapped_root = _mapped_host_path(root, settings)
    if path is None or not mapped_root:
        return False
    mapped_root = mapped_root.rstrip("/")
    if not mapped_root:
        return False
    return path == mapped_root or path.startswith(mapped_root + "/")


def _scope_under_resource(files: list[str], root: str | None, settings) -> bool:
    """Whether every marked file lies under the arr resource's path (#38)."""
    return all(_under_resource(path, root, settings) for path in files)


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
                    if not files:
                        raise VerificationError("empty_scope")
                    if not self._media_root_gate_open(files):
                        raise VerificationError("files_unknown")
                    snapshots = {}
                    for path in files:
                        try:
                            snapshots[path] = self.stat(path)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            # The preview refuses exactly what the real run
                            # refuses (#39), instead of promising it.
                            raise VerificationError("files_unknown") from None
                    if (not set(files)
                            <= set(snapshots) | self._seen_files(request)):
                        raise VerificationError("files_unknown")
                    if self._arr_responsible(request):
                        # The preview proves ownership exactly like the real
                        # run, with read-only requests, and never mutates.
                        resource = self._arr_preflight(request, files)
                        if resource is None:
                            # Nothing will be sent (#38/#39): a durable
                            # earlier attempt already saw every file and the
                            # resource has since disappeared, so the disk
                            # decides. The preview must report the outcome
                            # the real run would record, never a promise to
                            # delete something no run will delete.
                            presence = [self._presence(path) for path in files]
                            if FilePresence.UNKNOWN in presence:
                                raise VerificationError("files_unknown") from None
                            if FilePresence.PRESENT in presence:
                                raise VerificationError("files_present") from None
                except Exception as error:
                    diagnostic = _diagnostic(error)
                    code = diagnostic if diagnostic in FAILURE_CODES else "service"
                    # The preview reports the failure it would record, without
                    # mutating anything (S16 rule 2).
                    outcomes.append(DeletionOutcome(
                        request.id, DeletionStatus.FAILED, request,
                        error=f"removal.failed.{code}",
                    ))
                    continue
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
                if not self._media_root_gate_open(files):
                    # #39: the files are expected under a media root that
                    # cannot be read, so nothing can be verified, nothing
                    # is claimed and nothing is sent.
                    raise VerificationError("files_unknown")
                seen_now: set[str] = set()
                for path in files:
                    disk = self.hostfs.disk_for(path)
                    if disk:
                        disks.add(disk)
                    try:
                        snapshots[path] = self.stat(path)
                    except FileNotFoundError:
                        # "Already gone" is only proof for a file an earlier
                        # attempt saw (#39), checked per file below.
                        pass
                    except OSError:
                        # A failed pre-delete stat is never ignored.
                        raise VerificationError("files_unknown") from None
                    else:
                        seen_now.add(path)
                if not set(files) <= seen_now | self._seen_files(request):
                    # The rule is strict per file: every file of the scope
                    # must have been observed now or in an earlier attempt.
                    # A never-seen file means the environment is not what
                    # the app believes, so nothing is claimed and nothing
                    # is sent (#39).
                    raise VerificationError("files_unknown")
                if not self._claim(request):
                    if kill_switch_enabled(self.conn):
                        outcomes.append(DeletionOutcome(
                            request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request,
                        ))
                    continue
                claimed = True
                arr = self._arr_responsible(request)
                resource = self._arr_preflight(request, files) if arr else None
                # Ownership and per-file guards use only earlier evidence.
                # Commit new observations before any external mutation.
                self._record_seen_files(request, snapshots)
                if arr:
                    self._arr_delete(request, files, resource)
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
                # Preserve observations even when preflight refused; these
                # can only authorize a later attempt, never this one.
                self._record_seen_files(request, snapshots)
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

    def _media_root_gate_open(self, files: list[str]) -> bool:
        """Whether the media-root gate applies and passes (#39).

        The gate is only meaningful when the marked files are expected
        under the configured media root: a deployment that mounts its media
        elsewhere (or leaves the root unconfigured) must not be refused by
        it, because the per-file pre-stats remain the real guard.
        """
        media_root = getattr(self.engine.settings, "media_root", "")
        if not media_root:
            return True
        root = str(media_root).rstrip("/")
        if not root:
            return True
        if not all(path == root or path.startswith(root + "/") for path in files):
            return True
        return self.hostfs.is_mounted()

    # Durable per-file proof that a scope was seen on disk in an earlier
    # attempt (#39). The table is created lazily on the first successful
    # pre-stat of a real (non-dry) run, never during a preview.
    _SEEN_FILES_TABLE = (
        "CREATE TABLE IF NOT EXISTS removal_seen_files ("
        "removal_request_id INTEGER NOT NULL, file_path TEXT NOT NULL,"
        " seen_at TEXT NOT NULL,"
        " PRIMARY KEY (removal_request_id, file_path))"
    )

    def _record_seen_files(
        self, request: RemovalRequest, snapshots: dict[str, os.stat_result],
    ) -> None:
        """Persist which files this attempt really saw, for later retries.

        Evidence is bookkeeping, never a guard: only a missing table is
        created on demand, and any other database error is logged so it can
        never abort the batch or leave a request stuck mid-attempt.
        """
        if not snapshots:
            return
        now = _utc(self.engine.clock()).isoformat()
        rows = [(request.id, path, now) for path in sorted(snapshots)]
        insert = (
            "INSERT OR REPLACE INTO removal_seen_files"
            " (removal_request_id, file_path, seen_at) VALUES (?, ?, ?)"
        )
        try:
            try:
                self.conn.executemany(insert, rows)
            except sqlite3.OperationalError as error:
                if "no such table" not in str(error):
                    raise
                self.conn.execute(self._SEEN_FILES_TABLE)
                self.conn.executemany(insert, rows)
            # The commit sits inside the guard too: a failure here must
            # never escape, because the error handler of a failed attempt
            # records evidence again, and a second consecutive failure
            # would strand the request in "deleting" (MUST 2).
            self.conn.commit()
        except sqlite3.Error as error:
            self.conn.rollback()
            logger.warning(
                "removal_evidence_unrecordable",
                extra={"request_id": request.id, "error": str(error)},
            )

    def _seen_files(self, request: RemovalRequest) -> set[str]:
        """The files durable earlier attempts saw on disk (#39).

        The proof is per file and holds regardless of file sizes: one
        observed file never authorises "already gone" for a sibling, and
        a scope of zero-byte files is not excluded either.
        """
        try:
            rows = self.conn.execute(
                "SELECT file_path FROM removal_seen_files WHERE removal_request_id = ?",
                (request.id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row["file_path"] for row in rows}


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

    def _arr_preflight(self, request: RemovalRequest, files: list[str]):
        """Prove read-only that the arr owns every marked file (#38).

        Sends only GET requests, so the dry-run preview can run exactly
        this proof. Returns the resource, or ``None`` when a durable
        earlier attempt already saw every file and the resource has since
        disappeared (the disk then decides). Any refusal raises the fixed
        identity code and nothing is mutated.
        """
        item = self.conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        scope = set(files)
        if request.unit is Unit.MOVIE:
            if self.radarr is None or item["radarr_id"] is None:
                raise VerificationError("radarr_identity")
            try:
                movie = self.radarr.movie(item["radarr_id"])
            except RadarrNotFound:
                # A resource a durable earlier attempt already mutated may
                # be gone on retry: nothing is sent and the disk decides.
                # A stale id on a first attempt refuses instead (#38, #39).
                if set(files) <= self._seen_files(request):
                    return None
                raise VerificationError("radarr_identity") from None
            # The stored id is compared only when the app holds one: rows
            # linked by exact title+year, or behind a legacy agent guid,
            # carry none, and the mapped resource path owns the proof.
            if movie.tmdbId is None or (
                    item["tmdb_id"] is not None and movie.tmdbId != item["tmdb_id"]):
                raise VerificationError("radarr_identity")
            if not _scope_under_resource(files, movie.path, self.engine.settings):
                raise VerificationError("radarr_identity")
            return movie
        if self.sonarr is None or item["sonarr_id"] is None:
            raise VerificationError("sonarr_identity")
        try:
            series = self.sonarr.series(item["sonarr_id"])
        except SonarrNotFound:
            if set(files) <= self._seen_files(request):
                return None
            raise VerificationError("sonarr_identity") from None
        if series.tvdbId is None or (
                item["tvdb_id"] is not None and series.tvdbId != item["tvdb_id"]):
            raise VerificationError("sonarr_identity")
        if not _scope_under_resource(files, series.path, self.engine.settings):
            raise VerificationError("sonarr_identity")
        if request.unit is Unit.SEASONS:
            entries = self.sonarr.episode_files(item["sonarr_id"])
            mapped = {
                entry.id: _mapped_host_path(entry.path, self.engine.settings)
                for entry in entries
            }
            in_scope = [
                entry for entry in entries
                if entry.seriesId == item["sonarr_id"]
                and mapped[entry.id] in scope
            ]
            season_files = {
                mapped[entry.id] for entry in entries
                if entry.seasonNumber in set(request.seasons)
            }
            gone = {
                path for path in scope - season_files
                if path in self._seen_files(request)
                and self._presence(path) is FilePresence.GONE
            }
            # The arr must hold exactly the marked files for the marked
            # seasons: deleting a different set would remove files the
            # owner never marked (#38 scenario B).
            if season_files != scope - gone or len(in_scope) != len(scope - gone):
                raise VerificationError("sonarr_identity")
            if not season_files:
                return None
        return series

    def _arr_delete(self, request: RemovalRequest, files: list[str], resource) -> None:
        """Mutate the arr only after it provably owns every marked file (#38)."""
        item = self.conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        scope = set(files)
        if resource is None:
            return  # Already deleted by an earlier verified attempt.
        if request.unit is Unit.MOVIE:
            self.radarr.unmonitor_movie(item["radarr_id"])
            self.radarr.add_import_exclusion(resource.tmdbId, item["title"])
            self.radarr.delete_movie(item["radarr_id"], delete_files=True)
        elif request.unit is Unit.SEASONS:
            entries = self.sonarr.episode_files(item["sonarr_id"])
            self.sonarr.unmonitor_seasons(item["sonarr_id"], request.seasons)
            self.sonarr.delete_episode_files([
                entry.id for entry in entries
                if entry.seriesId == item["sonarr_id"]
                and _mapped_host_path(entry.path, self.engine.settings) in scope
            ])
        else:
            self.sonarr.unmonitor_series(item["sonarr_id"])
            self.sonarr.add_import_exclusion(resource.tvdbId, item["title"])
            self.sonarr.delete_series(item["sonarr_id"], delete_files=True)

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
