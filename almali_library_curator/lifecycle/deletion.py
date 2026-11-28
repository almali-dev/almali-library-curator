"""Execution of requests passed by the final check (S5, S6, S15).

The curator never unlinks media itself. Each removal is routed by unit
type (S15 rule 1): movies go through Radarr and series or seasons through
Sonarr when that service is configured and its deletion switch is on, and
through Plex otherwise. A dry run sends no mutating request at all and
does not perform even a collection cleanup. It shares the read-only arr
ownership and Plex metadata checks with execution, refusing multi-version
Plex items and checking disk presence for items Plex no longer knows.

The "deleted" claim is strict per file (#39, parent decision 2026-11-16):
every file of the marked scope must have been observed on disk now or by
an earlier attempt, and confirmed gone after the call. A scope holding a
file that was never visible fails with ``files_unknown`` and sends
nothing, even when a sibling file was observed: that means the environment
is not what the app believes (an offline array disk, a wrong path map),
and this tool refuses to act on facts it cannot see. The owner fixes the
environment and re-marks; removal events that recorded real removals stay
in the history, but the request's outcome is the failure.

A removal is also refused while another curated row still depends on its
target (#136): another ``media_items`` row carrying the same
``radarr_id``/``sonarr_id`` on that arr's route, or another row pointing at
a file of the marked scope. The arr call would take that library's files,
its monitoring and its import exclusion with it, so nothing is sent at all,
and the card names the other library until the owner decides which copy
goes. Linking still lets two rows share an arr id by design: the guard
belongs to the deletion, which is the only place that can destroy data.

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
from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum

from almali_library_curator.detection.models import Unit
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.lifecycle.engine import LifecycleEngine, _utc
from almali_library_curator.lifecycle.models import (
    AppliedEffect,
    DeletionOutcome, DeletionReport, DeletionStatus, RemovalRequest, RemovalState,
    RequestStateError,
    applied_effects_from_json,
    applied_effects_to_json,
)
from almali_library_curator.plex.client import (
    PlexAuthError, PlexClient, PlexError, PlexUnavailableError,
)
from almali_library_curator.plex.deletion import (
    PlexDeletionRefused, PlexMultiVersionError, PlexVersionChanged, PlexVersionParts,
    delete_media_item, delete_media_version, preflight_media_item,
    preflight_media_version,
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
# environment and mark the item again). ``shared_arr_entry`` and
# ``shared_file`` cover the sharing refusal of #136: another curated row
# still uses the arr entry this removal would delete, or a file of its
# scope. Both name the other library, so the screens can say which copy
# the owner has to decide about.
FAILURE_CODES = frozenset({
    "empty_scope", "files_unknown", "files_present", "interrupted", "seasons_missing",
    "radarr_identity", "sonarr_identity", "plex_refused", "plex_multi_version",
    "plex_auth", "plex_unavailable", "shared_arr_entry", "shared_file",
    "unsupported_scope",
    # #133: one version of a movie. ``radarr_managed_version`` is the owner's
    # decision D2: the file Radarr manages is never removed this way.
    "plex_version_changed", "plex_version_parts", "radarr_managed_version",
    "kept_version_missing",
})

#: The units every dispatch below knows. #133: a unit outside this set
#: (a version, say) must never fall through to the series path, which
#: would hand Sonarr ``delete_series`` or every episode to Plex.
KNOWN_UNITS = frozenset({Unit.MOVIE, Unit.SERIES, Unit.SEASONS})


def require_known_unit(unit) -> None:
    """Refuse a unit no dispatch here knows (``unsupported_scope``)."""
    if unit not in KNOWN_UNITS:
        raise VerificationError("unsupported_scope")

# Plex errors the owner can act on, most specific first (S15 rule 7).
_PLEX_DIAGNOSTICS = (
    (PlexDeletionRefused, "plex_refused"),
    (PlexVersionChanged, "plex_version_changed"),
    (PlexVersionParts, "plex_version_parts"),
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


# The two reasons a shared target is refused (#136), stored as
# ``removal.failed.<code>`` like every other preflight refusal.
SHARED_ARR_REASON = "shared_arr_entry"
SHARED_FILE_REASON = "shared_file"

# How many file paths one sharing query may carry: a long series holds more
# episode files than SQLite binds parameters in a statement, so the lookup
# walks the scope in chunks instead of refusing to look.
_SHARED_FILE_CHUNK = 400


class SharedUseRefusal(VerificationError):
    """A removal another curated row still depends on is refused (#136).

    ``code`` is the fixed diagnostic the request stores as
    ``removal.failed.<code>``; ``libraries`` names the other libraries, so
    the card and History can name the copy the owner has to decide about.
    The names are never baked into the stored value: the screens render the
    key in the reader's language and pass the names as its parameters
    (#47).
    """

    def __init__(self, code: str, libraries: tuple[str, ...]) -> None:
        super().__init__(code)
        self.code = code
        self.libraries = tuple(libraries)


def _library_titles(rows) -> tuple[str, ...]:
    """The library titles of a sharing lookup, each once, in query order."""
    titles: list[str] = []
    for row in rows:
        title = row["library"]
        if title and title not in titles:
            titles.append(title)
    return tuple(titles)


def _rows_for_paths(conn, sql: str, paths: list[str], extra: tuple) -> list:
    """Run a ``file_path IN (...)`` lookup over a long scope in chunks (#136)."""
    rows: list = []
    for start in range(0, len(paths), _SHARED_FILE_CHUNK):
        chunk = paths[start:start + _SHARED_FILE_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        rows.extend(conn.execute(sql.format(marks=marks), (*chunk, *extra)).fetchall())
    return rows


def scope_files(conn, *, media_item_id: int, unit: Unit, seasons, rating_key: str) -> list[str]:
    """The container paths exactly the request's scope consists of.

    A seasons scope whose season rows are missing is its own failure
    (S16 rule 2): it is never treated as the series scope, because a
    series-scoped delete would remove more than the owner marked.
    """
    require_known_unit(unit)
    if unit is Unit.MOVIE:
        rows = conn.execute(
            "SELECT file_path FROM media_items WHERE id = ? AND plex_present = 1", (media_item_id,)
        )
        files = {row["file_path"] for row in rows if row["file_path"]}
    else:
        rows = conn.execute(
            "SELECT file_path, season_number FROM episodes WHERE series_rating_key = ? AND plex_present = 1",
            (rating_key,),
        )
        if unit is Unit.SEASONS:
            wanted = set(seasons)
            scope_rows = [
                row for row in rows if row["season_number"] in wanted
            ]
            # Every marked season must still have both its container
            # row and episode rows. A nonempty subset is not proof of
            # the whole scope: the backend could delete unverified files.
            season_rows = conn.execute(
                "SELECT season_number FROM media_items"
                " WHERE parent_rating_key = ? AND item_type = 'season' AND plex_present = 1",
                (rating_key,),
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


def shared_use_conflict(
    conn, media_item_id: int, unit: Unit, files: list[str], *, arr_route: bool = True,
) -> SharedUseRefusal | None:
    """The other curated rows that still use this removal's target (#136).

    S1: another ``media_items`` row carries the same ``radarr_id`` (movie)
    or ``sonarr_id`` (series or seasons), when that arr really is the route
    of this removal. S2: another row points at a file of the marked scope,
    whatever the route. Either way the call would take the other copy's
    files, monitoring or exclusion with it, so the removal is refused
    before anything is sent.
    """
    if unit is Unit.VERSION:
        # #133: a version is always removed through Plex, so only the file
        # rule (S2) applies; the movie's own arr entry is never the route.
        unit, arr_route = Unit.MOVIE, False
    require_known_unit(unit)
    item = conn.execute(
        "SELECT radarr_id, sonarr_id, item_type, plex_rating_key"
        " FROM media_items WHERE id = ?", (media_item_id,),
    ).fetchone()
    if item is None:
        return None
    column, value = (
        ("radarr_id", item["radarr_id"]) if unit is Unit.MOVIE
        else ("sonarr_id", item["sonarr_id"])
    )
    if arr_route and value is not None:
        rows = conn.execute(
            "SELECT mi.id, l.title AS library FROM media_items mi"
            " LEFT JOIN libraries l ON l.id = mi.library_id"
            f" WHERE mi.id <> ? AND mi.{column} = ? AND mi.plex_present = 1"
            " ORDER BY l.title, mi.id",
            (media_item_id, value),
        ).fetchall()
        if rows:
            return SharedUseRefusal(SHARED_ARR_REASON, _library_titles(rows))
    paths = sorted({path for path in files if path})
    if not paths:
        return None
    # The item's own rows are not "another row": its season containers
    # belong to the same scope carrier, and a series' own episodes hold
    # the very files the scope names.
    series_key = item["plex_rating_key"] if item["item_type"] != "movie" else None
    titles = [
        row["library"] for row in _rows_for_paths(
            conn,
            "SELECT mi.id, mi.parent_rating_key, l.title AS library"
            " FROM media_items mi LEFT JOIN libraries l ON l.id = mi.library_id"
            " WHERE mi.file_path IN ({marks}) AND mi.plex_present = 1",
            paths, (),
        )
        if row["id"] != media_item_id
        and not (series_key is not None and row["parent_rating_key"] == series_key)
    ]
    titles.extend(
        row["library"] for row in _rows_for_paths(
            conn,
            "SELECT l.title AS library FROM episodes e"
            " LEFT JOIN media_items mi ON mi.plex_rating_key = e.series_rating_key"
            " LEFT JOIN libraries l ON l.id = mi.library_id"
            " WHERE e.file_path IN ({marks}) AND e.plex_present = 1"
            " AND e.series_rating_key <> ?",
            paths, (series_key or "",),
        )
    )
    libraries = tuple(dict.fromkeys(title for title in titles if title))
    if libraries or titles:
        # A sharing row whose library row is gone still refuses: the code
        # alone, rendered with the generic "another library" phrase.
        return SharedUseRefusal(SHARED_FILE_REASON, libraries)
    return None


def shared_use_for_scope(
    conn, media_item_id: int, unit: Unit, seasons, *, arr_route: bool = True,
) -> SharedUseRefusal | None:
    """Re-derive the sharing refusal of a failed scope for the screens (#136).

    The card and History store only the reason key, so they name the other
    library by running the same read-only check again. Nothing was mutated
    by the refusal, so the sharing rows are still there to name. A scope
    whose seasons are unknown (a History row) falls back to the series
    scope: the arr entry and every sharing file it can find are the same.
    """
    row = conn.execute(
        "SELECT plex_rating_key FROM media_items WHERE id = ?", (media_item_id,),
    ).fetchone()
    if row is None:
        return None
    unit = Unit.SERIES if unit is Unit.SEASONS and not seasons else unit
    try:
        files = scope_files(
            conn, media_item_id=media_item_id, unit=unit,
            seasons=tuple(seasons), rating_key=row["plex_rating_key"],
        )
    except VerificationError:
        return None
    return shared_use_conflict(conn, media_item_id, unit, files, arr_route=arr_route)


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
        # #148 S5: the Sonarr episode files of the one removal in flight, so
        # its read-only proof and its delete share a single read.
        self._episode_files_cache: dict[tuple[int, int], list] = {}

    def _episode_files(self, request: RemovalRequest, sonarr_id: int) -> list:
        """S5 (#148): Sonarr's episode files of one seasons removal, read once.

        The proof of ownership and the delete itself need the same list, and
        the attempt only reads between them, so one read answers both. The
        cache belongs to that one attempt: ``run_deletions`` empties it before
        it starts a removal, so a second removal is never answered from the
        first one's list.
        """
        key = (request.id, sonarr_id)
        cached = self._episode_files_cache.get(key)
        if cached is None:
            cached = self._episode_files_cache[key] = self.sonarr.episode_files(sonarr_id)
        return cached

    def run_deletions(self, *, passed_ids=()) -> DeletionReport:
        """Only the caller's fresh final-check passes may be executed.

        Dry-run passes still have their original state because the check is
        non-persisting. An empty allowlist never authorizes a deletion.
        """
        outcomes = [] if self.engine.settings.dry_run else self._recover_interrupted()
        rows = [{"id": identifier} for identifier in sorted(set(passed_ids))]
        for row in rows:
            request_id = row["id"]
            # A new attempt: nothing read for the previous removal may answer.
            self._episode_files_cache.clear()
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
            if request.unit is Unit.VERSION:
                # #133: its own branch, always through Plex (design §2.5).
                outcome = self._run_version(request)
                if outcome is not None:
                    outcomes.append(outcome)
                continue
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
                    # #136: a target another curated row still uses is
                    # refused here too, before the first read-only request.
                    self._shared_preflight(request, files)
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
                    else:
                        self._plex_preflight(request)
                except Exception as error:
                    # The preview reports the failure it would record, without
                    # mutating anything (S16 rule 2), never external error text.
                    diagnostic = _diagnostic(error)
                    code = diagnostic if diagnostic in FAILURE_CODES else "service"
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
                # #136: refuse before the claim and before any request to
                # Radarr, Sonarr or Plex. Nothing is deleted, unmonitored or
                # excluded while another curated row still uses the target.
                self._shared_preflight(request, files)
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

    def _run_version(self, request: RemovalRequest) -> DeletionOutcome | None:
        """#133: remove one version of a movie through Plex, and only that file.

        The same proofs as every removal, in the same order: the media-root
        gate and the strict per-file "seen" rule, the shared-file refusal,
        then read-only checks before the claim. A version is never routed
        through Radarr, whose movie delete would take every version; when
        Radarr is configured, the file it manages is never removed (S3, the
        owner's decision D2) and a Radarr that cannot say which file it
        manages refuses. Plex reads the item again first. "Deleted" is
        claimed only when the removed file is gone and every kept version
        is still on disk. ``None`` means the owner cancelled meanwhile.
        """
        dry_run = self.engine.settings.dry_run
        if not dry_run and kill_switch_enabled(self.conn):
            return DeletionOutcome(request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request)
        target = request.version_file_path or ""
        files = [target]
        snapshots: dict[str, os.stat_result] = {}
        disks: set[str] = set()
        claimed = False
        try:
            if not target or request.version_media_id is None:
                raise VerificationError("plex_version_changed")
            if self.plex is None:
                raise VerificationError("plex_unavailable")
            if not self._media_root_gate_open(files):
                raise VerificationError("files_unknown")
            kept = self._kept_version_files(request)
            if not kept:
                raise VerificationError("plex_version_changed")
            disk = self.hostfs.disk_for(target)
            if disk:
                disks.add(disk)
            try:
                snapshots[target] = self.stat(target)
            except FileNotFoundError:
                pass
            except OSError:
                raise VerificationError("files_unknown") from None
            if target not in snapshots and target not in self._seen_files(request):
                raise VerificationError("files_unknown")
            presence = [self._presence(path) for path in kept]
            if FilePresence.UNKNOWN in presence:
                raise VerificationError("files_unknown")
            if FilePresence.GONE in presence:
                # Removing this one would leave fewer versions than the
                # owner was shown, maybe none at all.
                raise VerificationError("kept_version_missing")
            self._shared_preflight(request, files)
            self._refuse_radarr_managed(request, target)
            rating_key = self.engine._rating_key(request.media_item_id)
            if not preflight_media_version(
                self.plex, rating_key, request.version_media_id, target
            ):
                raise VerificationError("plex_version_changed")
            label = self._version_label(request)
            if dry_run:
                size, _ = self._space(snapshots)
                return DeletionOutcome(request.id, DeletionStatus.WOULD_DELETE, request,
                                       size_bytes=size, files=tuple(files))
            if not self._claim(request):
                if kill_switch_enabled(self.conn):
                    return DeletionOutcome(
                        request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request)
                return None
            claimed = True
            self._record_seen_files(request, snapshots)
            delete_media_version(self.plex, rating_key, request.version_media_id, target)
            after = self._presence(target)
            if after is FilePresence.UNKNOWN:
                raise VerificationError("files_unknown")
            if after is FilePresence.PRESENT:
                raise VerificationError("files_present")
            if any(self._presence(path) is not FilePresence.PRESENT for path in kept):
                logger.error("removal_kept_version_missing",
                             extra={"request_id": request.id})
                raise VerificationError("kept_version_missing")
            size, freed = self._space(snapshots)
            return self._finish(request, size, freed, ", ".join(sorted(disks)) or None,
                                version_label=label)
        except Exception as error:
            diagnostic = _diagnostic(error)
            if dry_run:
                code = diagnostic if diagnostic in FAILURE_CODES else "service"
                return DeletionOutcome(request.id, DeletionStatus.FAILED, request,
                                       error=f"removal.failed.{code}")
            self._record_seen_files(request, snapshots)
            if not claimed and not self._claim(request):
                if kill_switch_enabled(self.conn):
                    return DeletionOutcome(
                        request.id, DeletionStatus.BLOCKED_BY_KILL_SWITCH, request)
                return None
            size, _ = self._space(snapshots)
            return self._fail(request, diagnostic, size, ", ".join(sorted(disks)) or None)

    def _kept_version_files(self, request: RemovalRequest) -> list[str]:
        """The files of the movie's other versions, as the last sync stored them."""
        rows = self.conn.execute(
            "SELECT plex_media_id, file_path FROM media_versions WHERE media_item_id = ?"
            " ORDER BY position", (request.media_item_id,),
        ).fetchall()
        return sorted({
            row["file_path"] for row in rows
            if row["file_path"] and row["plex_media_id"] != request.version_media_id
            and row["file_path"] != request.version_file_path
        })

    def _refuse_radarr_managed(self, request: RemovalRequest, target: str) -> None:
        """S3: never remove the file Radarr manages; refuse when it cannot tell.

        Read-only. Radarr counts as configured whether or not it may delete:
        a monitored movie whose file vanished would be downloaded again.
        """
        if self.radarr is None:
            return
        item = self.conn.execute(
            "SELECT radarr_id FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        if item is None or item["radarr_id"] is None:
            raise VerificationError("radarr_identity")
        try:
            managed = self.radarr.managed_file(item["radarr_id"])
        except Exception:
            raise VerificationError("radarr_identity") from None
        if managed is not None and _mapped_host_path(managed, self.engine.settings) == target:
            raise VerificationError("radarr_managed_version")

    def _version_label(self, request: RemovalRequest) -> str | None:
        """The History label of the removed version, e.g. "1080 · h264"."""
        row = self.conn.execute(
            "SELECT resolution, video_codec FROM media_versions"
            " WHERE media_item_id = ? AND plex_media_id = ? AND file_path = ?",
            (request.media_item_id, request.version_media_id, request.version_file_path),
        ).fetchone()
        if row is None:
            return None
        parts = [part for part in (row["resolution"], row["video_codec"]) if part]
        return " · ".join(parts) or None

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
        """The container paths exactly the request's scope consists of."""
        return scope_files(
            self.conn, media_item_id=request.media_item_id, unit=request.unit,
            seasons=request.seasons,
            rating_key=self.engine._rating_key(request.media_item_id),
        )

    def _shared_preflight(self, request: RemovalRequest, files: list[str]) -> None:
        """Refuse a removal another curated row still depends on (#136).

        Read-only, so the preview runs it exactly like the real run, and it
        runs before the first request to Radarr, Sonarr or Plex: a shared
        target deletes nothing, unmonitors nothing and adds no exclusion.
        The *arr entry only matters when that arr is the route: a removal
        Plex performs touches only its own rating keys.
        """
        # #133: a version is always removed through Plex, never an arr.
        arr_route = (False if request.unit is Unit.VERSION
                     else self._arr_responsible(request))
        conflict = shared_use_conflict(
            self.conn, request.media_item_id, request.unit, files, arr_route=arr_route)
        if conflict is not None:
            raise conflict

    def _arr_responsible(self, request: RemovalRequest) -> bool:
        """S15 rules 1-2: a configured arr with its switch on owns its unit type.

        A configured and enabled arr wins even when it cannot handle the
        item: the removal then fails and retries, never falling back to
        Plex, because that arr could download the item again.
        """
        settings = self.engine.settings
        require_known_unit(request.unit)
        if request.unit is Unit.MOVIE:
            return self.radarr is not None and settings.radarr_deletes
        return self.sonarr is not None and settings.sonarr_deletes

    def _plex_keys(self, request: RemovalRequest) -> list[str]:
        """The Plex items whose deletion removes exactly the scope (S15 rule 4).

        A movie is the movie itself. A season or a series is one key per
        episode, so the series and season containers are never deleted.
        """
        require_known_unit(request.unit)
        if request.unit is Unit.MOVIE:
            rows = self.conn.execute(
                "SELECT plex_rating_key FROM media_items WHERE id = ? AND plex_present = 1",
                (request.media_item_id,),
            )
            return [row["plex_rating_key"] for row in rows]
        rows = self.conn.execute(
            "SELECT plex_rating_key, season_number FROM episodes"
            " WHERE series_rating_key = ? AND plex_present = 1 ORDER BY season_number, episode_number",
            (self.engine._rating_key(request.media_item_id),),
        )
        wanted = set(request.seasons)
        return [
            row["plex_rating_key"] for row in rows
            if request.unit is Unit.SERIES or row["season_number"] in wanted
        ]

    def _plex_preflight(self, request: RemovalRequest) -> None:
        """Preview the Plex route using only GETs and local file checks.

        A missing Plex item causes no DELETE in execution either, so its
        files must already be gone. Known siblings may still be present:
        execution will delete those, not the missing item's files.
        """
        if self.plex is None:
            raise RequestStateError("Plex is unavailable")
        keys = self._plex_keys(request)
        if not keys:
            raise VerificationError("empty_scope")
        missing = []
        for key in keys:
            if not preflight_media_item(self.plex, key):
                table = "media_items" if request.unit is Unit.MOVIE else "episodes"
                rows = self.conn.execute(
                    f"SELECT file_path FROM {table} WHERE plex_rating_key = ?", (key,),
                )
                missing.extend(row["file_path"] for row in rows if row["file_path"])
        presence = [self._presence(path) for path in missing]
        if FilePresence.UNKNOWN in presence:
            raise VerificationError("files_unknown")
        if FilePresence.PRESENT in presence:
            raise VerificationError("files_present")

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
        require_known_unit(request.unit)
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
            entries = self._episode_files(request, item["sonarr_id"])
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
        """Mutate the arr only after it provably owns every marked file (#38).

        Every side effect is durably recorded before the arr call that
        applies it, from the state read just before that call, so a crash
        in the window between the call and its record still leaves a
        truthful record a cancel can revert (#107). Only an effect this
        request will really apply is recorded: a title the owner already
        unmonitored, or an exclusion that was already on the list, is not
        the removal's to undo (#103), and a call whose answer says it
        changed nothing drops the record again. The resource delete itself
        needs no record: once it succeeds the removal is verified on disk
        and nothing is left to revert.
        """
        item = self.conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        scope = set(files)
        if resource is None:
            return  # Already deleted by an earlier verified attempt.
        require_known_unit(request.unit)
        if request.unit is Unit.MOVIE:
            self._unmonitor_movie(request, item["radarr_id"])
            self._exclude_movie(request, resource, item["title"])
            self.radarr.delete_movie(item["radarr_id"], delete_files=True)
        elif request.unit is Unit.SEASONS:
            entries = self._episode_files(request, item["sonarr_id"])
            self._unmonitor_seasons(request, item["sonarr_id"], request.seasons)
            self.sonarr.delete_episode_files([
                entry.id for entry in entries
                if entry.seriesId == item["sonarr_id"]
                and _mapped_host_path(entry.path, self.engine.settings) in scope
            ])
        elif request.unit is Unit.SERIES:
            self._unmonitor_series(request, item["sonarr_id"])
            self._exclude_series(request, resource, item["title"])
            self.sonarr.delete_series(item["sonarr_id"], delete_files=True)

    def _unmonitor_movie(self, request: RemovalRequest, radarr_id: int) -> None:
        """Stop monitoring the movie, on record before the write (#107).

        The flag is read first, so a movie the owner already unmonitored
        is never recorded (#103). The call's own answer then settles the
        record: a write that changed nothing drops it again, so a read
        that went stale can never make a cancel monitor a title the owner
        left unmonitored.
        """
        pending = AppliedEffect(kind="unmonitor", arr="radarr", resource_id=radarr_id)
        recorded = None
        if self.radarr.movie_monitored(radarr_id):
            recorded = pending
            self._record_effect(request, recorded)
        try:
            change = self.radarr.unmonitor_movie(radarr_id)
        except Exception:
            self._settle_failed_call(
                request, recorded,
                lambda: None if self.radarr.movie_monitored(radarr_id) else pending,
            )
            raise
        self._settle_effect(request, recorded, pending if change.changed else None)

    def _unmonitor_series(self, request: RemovalRequest, sonarr_id: int) -> None:
        """Stop monitoring the series, on record before the write (#107)."""
        pending = AppliedEffect(kind="unmonitor", arr="sonarr", resource_id=sonarr_id)
        recorded = None
        if self.sonarr.series_monitored(sonarr_id):
            recorded = pending
            self._record_effect(request, recorded)
        try:
            change = self.sonarr.unmonitor_series(sonarr_id)
        except Exception:
            self._settle_failed_call(
                request, recorded,
                lambda: None if self.sonarr.series_monitored(sonarr_id) else pending,
            )
            raise
        self._settle_effect(request, recorded, pending if change.changed else None)

    def _unmonitor_seasons(
        self, request: RemovalRequest, sonarr_id: int, seasons: tuple[int, ...],
    ) -> None:
        """Stop monitoring exactly these seasons, on record before the write (#107).

        Only the seasons the series still monitors are named, so a season
        the owner had already unmonitored is not recorded (#103), and the
        record is narrowed to the seasons the season pass really flipped.
        """
        wanted = tuple(seasons)
        monitored = self.sonarr.monitored_seasons(sonarr_id, wanted)
        pending = AppliedEffect(
            kind="unmonitor", arr="sonarr", resource_id=sonarr_id, seasons=monitored,
        )
        recorded = None
        if monitored:
            recorded = pending
            self._record_effect(request, recorded)
        try:
            change = self.sonarr.unmonitor_seasons(sonarr_id, wanted)
        except Exception:

            def flipped() -> AppliedEffect | None:
                still = set(self.sonarr.monitored_seasons(sonarr_id, monitored))
                landed = tuple(season for season in monitored if season not in still)
                return replace(pending, seasons=landed) if landed else None

            self._settle_failed_call(request, recorded, flipped)
            raise
        applied = replace(pending, seasons=change.seasons) if change.seasons else None
        self._settle_effect(request, recorded, applied)

    def _exclude_movie(self, request: RemovalRequest, resource, title: str) -> None:
        """Add the movie's import exclusion, on record before the POST (#107).

        An exclusion the list already holds is the owner's, so nothing is
        recorded and a cancel leaves it alone (#103). The record written
        before the POST names the external id only; the arr id is filled in
        once the POST answers it, so a cancel that meets a record without
        it looks the entry up by external id instead.
        """
        pending = AppliedEffect(
            kind="exclusion", arr="radarr", external_id=resource.tmdbId, title=title,
        )
        recorded = None
        if self.radarr.find_import_exclusion(resource.tmdbId) is None:
            recorded = pending
            self._record_effect(request, recorded)
        try:
            change = self.radarr.add_import_exclusion(
                resource.tmdbId, title, resource.year,
            )
        except Exception:
            self._settle_failed_call(
                request, recorded,
                lambda: pending
                if self.radarr.find_import_exclusion(resource.tmdbId) else None,
            )
            raise
        applied = (
            replace(pending, exclusion_id=change.exclusion.id)
            if change.created else None
        )
        self._settle_effect(request, recorded, applied)

    def _exclude_series(self, request: RemovalRequest, resource, title: str) -> None:
        """Add the series' import exclusion, on record before the POST (#107)."""
        pending = AppliedEffect(
            kind="exclusion", arr="sonarr", external_id=resource.tvdbId, title=title,
        )
        recorded = None
        if self.sonarr.find_import_exclusion(resource.tvdbId) is None:
            recorded = pending
            self._record_effect(request, recorded)
        try:
            change = self.sonarr.add_import_exclusion(resource.tvdbId, title)
        except Exception:
            self._settle_failed_call(
                request, recorded,
                lambda: pending
                if self.sonarr.find_import_exclusion(resource.tvdbId) else None,
            )
            raise
        applied = (
            replace(pending, exclusion_id=change.exclusion.id)
            if change.created else None
        )
        self._settle_effect(request, recorded, applied)

    def _settle_failed_call(
        self, request: RemovalRequest, recorded: AppliedEffect | None,
        landed: Callable[[], AppliedEffect | None],
    ) -> None:
        """Settle the record of an arr call that raised (#107).

        A fresh read says what landed; when nothing did, or the read fails
        too, the intent is dropped, so a cancel never claims an exclusion or
        unmonitor the owner makes afterwards. Only a crash keeps an intent.
        """
        if recorded is None:
            return
        try:
            applied = landed()
        except Exception:
            applied = None
        self._settle_effect(request, recorded, applied)

    def _update_effects(
        self, request: RemovalRequest,
        edit: Callable[[tuple[AppliedEffect, ...]], tuple[AppliedEffect, ...]],
    ) -> None:
        """Read-modify-write the applied-effects record and commit it (#69).

        Committed before the next arr call, so every side effect this
        removal holds is durable exactly when the arr holds it. An edit
        that changes nothing writes nothing.
        """
        row = self.conn.execute(
            "SELECT applied_effects FROM removal_requests WHERE id = ?",
            (request.id,),
        ).fetchone()
        applied = applied_effects_from_json(row["applied_effects"]) if row else ()
        updated = edit(applied)
        if updated == applied:
            return
        self.conn.execute(
            "UPDATE removal_requests SET applied_effects = ? WHERE id = ?",
            (applied_effects_to_json(updated), request.id),
        )
        self.conn.commit()

    def _record_effect(self, request: RemovalRequest, effect: AppliedEffect) -> None:
        """Record one arr side effect durably, before the call applies it (#107).

        Committed before the arr call, so an attempt that dies before it
        writes anything afterwards — or that fails later, on the delete
        itself or on disk verification — leaves a truthful record a
        cancellation can revert from. The record holds what is applied, not
        an audit log: a retry that re-records an effect an earlier attempt
        already recorded keeps it once, and a call that changed nothing at
        all drops it again, so a cancel cannot revert a setting the owner
        made before the removal (#103).
        """
        self._update_effects(
            request,
            lambda applied: applied if effect in applied else applied + (effect,),
        )

    def _drop_effect(self, request: RemovalRequest, effect: AppliedEffect) -> None:
        """Forget an effect the call it was recorded for did not apply (#107)."""
        self._update_effects(
            request,
            lambda applied: tuple(entry for entry in applied if entry != effect),
        )

    def _replace_effect(
        self, request: RemovalRequest, recorded: AppliedEffect, applied: AppliedEffect,
    ) -> None:
        """Record what the call really applied in place of the pre-call guess (#107)."""
        def edit(applied_effects: tuple[AppliedEffect, ...]) -> tuple[AppliedEffect, ...]:
            updated = tuple(
                applied if entry == recorded else entry for entry in applied_effects
            )
            if applied not in updated:
                updated += (applied,)
            return updated

        self._update_effects(request, edit)

    def _settle_effect(
        self, request: RemovalRequest, recorded: AppliedEffect | None,
        applied: AppliedEffect | None,
    ) -> None:
        """Keep exactly what the arr call applied (#107).

        ``recorded`` is the effect already on record, or ``None`` when the
        pre-read said the call would change nothing. ``applied`` is what
        the call's own answer names, or ``None`` when it changed nothing.
        """
        if applied is None:
            if recorded is not None:
                self._drop_effect(request, recorded)
        elif recorded is None:
            self._record_effect(request, applied)
        else:
            self._replace_effect(request, recorded, applied)

    def _finish(
        self, request: RemovalRequest, size: int, freed: int, disk: str | None,
        *, version_label: str | None = None,
    ) -> DeletionOutcome:
        now = _utc(self.engine.clock()).isoformat()
        cursor = self.conn.execute(
            "UPDATE removal_requests SET state = ?, deleted_at = ?, last_error = NULL,"
            " applied_effects = NULL, updated_at = ? WHERE id = ? AND state = ?",
            (RemovalState.DELETED.value, now, now, request.id, RemovalState.DELETING.value),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise RequestStateError("Request changed during deletion")
        self._record_event(request, "deleted", now, size, freed, disk,
                           version_label=version_label)
        self.conn.commit()
        self.engine.retry_collection_cleanup()
        logger.info("removal_deleted", extra={"request_id": request.id, "freed_bytes": freed})
        return DeletionOutcome(request.id, DeletionStatus.DELETED,
                               self.engine._request_by_id(request.id), size, freed)

    def _record_event(
        self, request: RemovalRequest, outcome: str, now: str, size: int,
        freed: int, disk: str | None, error: str | None = None,
        *, version_label: str | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT reason, reason_parts FROM removal_requests WHERE id = ?", (request.id,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO removal_events (removal_request_id, media_item_id, outcome,"
            " title, unit, category, reason, size_bytes, freed_bytes, disk, marked_by,"
            " error, occurred_at, reason_parts, version_label)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request.id, request.media_item_id, outcome, self.engine._history_title(request),
             request.unit.value, request.category.value, row["reason"], size, freed, disk,
             request.marked_by, error, now, row["reason_parts"], version_label),
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
