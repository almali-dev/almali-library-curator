"""The removal lifecycle engine (S5).

Nothing is deleted here. The engine moves a candidate through the first
four lifecycle stages — marked, in the "Leaving soon" collection for a
grace period, then a final re-check — and records every cancellation
with a localized reason. Deletion itself is the next stage (T7, S5
step 5).

The engine is pure orchestration over the synced database plus three
injected ports, each optional per S2: a collections port
(:class:`PlexClient` implements it), a protection oracle
(:class:`~almali_library_curator.protection.ProtectionEngine` satisfies
it) and a rematcher (:class:`~almali_library_curator.lifecycle.DetectionRematcher`
is the production adapter). An absent port degrades: no collection, not
protected, still matching.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Protocol

from almali_library_curator.config import Settings
from almali_library_curator import library_scope
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from almali_library_curator.i18n import DEFAULT_LOCALE
from almali_library_curator.lifecycle.models import (
    AppliedEffect,
    CancelCode,
    DuplicateMarkError,
    GraceRangeError,
    LifecycleError,
    ProtectedItemError,
    RemovalRequest,
    RemovalState,
    RequestStateError,
    RecheckOutcome,
    RecheckReport,
    SideEffectRevertError,
    UncuratedItemError,
    applied_effects_from_json,
    applied_effects_to_json,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

# S5 step 2: the grace period defaults to 14 days and accepts 3 to 60.
MIN_GRACE_DAYS = 3
# A cancellation claim older than this is left over from a crash (#99): a
# live claim only lasts while the arr requests of its revert run, and those
# are bounded by the clients' timeouts and retries.
STALE_CANCELLATION_CLAIM = timedelta(minutes=30)
MAX_GRACE_DAYS = 60


class CollectionsPort(Protocol):
    """The Plex collection writes the lifecycle needs (S5 step 3).

    :class:`~almali_library_curator.plex.PlexClient` implements it. The
    ``section_key`` is the library the item lives in — the engine
    resolves it from the synced database — and the returned key is the
    collection's Plex rating key.
    """

    def ensure_collection(self, section_key: str, name: str) -> str: ...

    def add_to_collection(self, collection_key: str, rating_key: str) -> None: ...

    def remove_from_collection(self, collection_key: str, rating_key: str) -> None: ...


class ProtectionOracle(Protocol):
    """Anything answering what protection covers a candidate (S4).

    ``ProtectionEngine.protection_for`` satisfies it; the engine holds
    no import of the protection package.
    """

    def protection_for(self, candidate: Candidate) -> object | None: ...


class Rematcher(Protocol):
    """Anything answering whether a candidate still matches (S5 step 4)."""

    def still_matches(self, candidate: Candidate) -> bool: ...


class Reverter(Protocol):
    """Anything undoing one applied arr side effect (#69).

    :class:`~almali_library_curator.lifecycle.reverts.ArrReverter`
    satisfies it over the real arr clients. A revert that cannot finish
    raises :class:`SideEffectRevertError` carrying the effect, so the
    engine keeps it in the record of what is still applied.
    """

    def revert(self, effect: AppliedEffect) -> None: ...


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def cancel_reason_text(
    locale: str, code: str, params: Mapping[str, object] | None = None
) -> str:
    """A cancellation reason in ``locale``, from its code and raw parameters.

    The parameters are stored raw (a name, an ISO date) and only formatted
    here, at render time, so the same stored cancellation answers in the
    scan's language as the fallback text and in the reader's language on
    screen (#47, S1a).
    """
    rendered: dict[str, object] = {}
    for name, value in (params or {}).items():
        moment = _parse_iso(str(value)) if name == "date" else None
        rendered[name] = text.day_month(locale, moment) if moment is not None else value
    return text.render(locale, f"removal.cancel.{code}", **rendered)


def _cancel_params(raw: str | None) -> dict[str, object] | None:
    """The stored raw parameters of a cancellation, or ``None`` if absent.

    A damaged record reads as absent: the stored text is still a truthful
    reason, so a row is never refused over its parameters (#47, S1d).
    """
    if not raw:
        return None
    try:
        params = json.loads(raw)
    except ValueError:
        return None
    return params if isinstance(params, dict) else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class LifecycleEngine:
    """Runs the removal lifecycle of S5 over the synced database."""

    # Undo and Keep it are allowed only before execution is claimed.
    # DELETING is open for collection ownership, but no longer cancelable.
    CANCELABLE_STATES = (
        RemovalState.MARKED, RemovalState.RECHECK_PASSED, RemovalState.REMOVAL_FAILED
    )

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings,
        clock: Clock,
        collections: CollectionsPort | None = None,
        protection: ProtectionOracle | None = None,
        rematcher: Rematcher | None = None,
        reverter: Reverter | None = None,
        cleanup_backlog_on_cancel: bool = True,
    ) -> None:
        self.conn = conn
        # #155: a cancellation sweeps every pending collection cleanup only
        # when the caller is the scan or the executor. The web request path
        # (Undo) cleans up its own request alone; the backlog stays with the
        # retry the scan already runs.
        self.cleanup_backlog_on_cancel = cleanup_backlog_on_cancel
        self.settings = settings
        self.clock = clock
        self.collections = collections
        self.protection = protection
        self.rematcher = rematcher
        self.reverter = reverter
        self.locale = settings.locale or DEFAULT_LOCALE

    # -- marking (S5 step 2) ---------------------------------------------

    def mark_for_removal(
        self,
        candidate: Candidate,
        *,
        grace_days: int | None = None,
        marked_by: str = "owner",
    ) -> RemovalRequest:
        """The owner marks a candidate; the grace period starts.

        The request and its scope carrier (a ``candidates`` row whose
        ``facts`` hold the unit, the seasons and every matching fact)
        are persisted in one commit, so a rejected mark leaves nothing
        behind.
        """
        days = self.settings.grace_days if grace_days is None else grace_days
        if not MIN_GRACE_DAYS <= days <= MAX_GRACE_DAYS:
            raise GraceRangeError(
                f"grace_days must be between {MIN_GRACE_DAYS} and {MAX_GRACE_DAYS},"
                f" got {days}"
            )
        if not candidate.matches:
            raise LifecycleError("a candidate with no matching category cannot be marked")
        if (
            self._scope_is_gone(candidate.media_item_id, candidate.unit, candidate.seasons)
            or not library_scope.item_is_curated(
                self.conn, self.settings, candidate.media_item_id
            )
        ):
            raise UncuratedItemError(
                f"{candidate.title} is absent from Plex or outside the curated scope"
                " and cannot be marked (S14 rule 6)"
            )
        if (
            self.protection is not None
            and self.protection.protection_for(candidate) is not None
        ):
            raise ProtectedItemError(
                f"{candidate.title} is protected and cannot be marked for removal"
            )
        if (
            self._open_scope_request(
                candidate.media_item_id, candidate.unit, candidate.seasons
            )
            is not None
            or self._has_unreadable_open_request(candidate.media_item_id)
        ):
            raise DuplicateMarkError(
                f"{candidate.title} ({candidate.unit.value},"
                f" seasons {sorted(candidate.seasons)}) already has an open"
                " removal request"
            )

        marked_at = _utc(self.clock())
        due_at = marked_at + timedelta(days=days)
        primary = candidate.matches[0].category
        reason = "; ".join(candidate.reasons)
        # #47: the same sentence as a key plus raw parameters, rendered per
        # screen; the text stays beside it for older readers.
        reason_parts = json.dumps(list(candidate.reason_parts))
        facts = {
            "unit": candidate.unit.value,
            "seasons": sorted(set(candidate.seasons)),
            "matches": dict(candidate.facts.get("matches") or {}),
            "collection_members": [],
        }
        try:
            cursor = self.conn.execute(
                "INSERT INTO candidates"
                " (media_item_id, category, reason, facts, state, detected_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'candidate', ?, ?)",
                (
                    candidate.media_item_id,
                    primary.value,
                    reason,
                    json.dumps(facts),
                    marked_at.isoformat(),
                    marked_at.isoformat(),
                ),
            )
            candidate_id = int(cursor.lastrowid)
            cursor = self.conn.execute(
                "INSERT INTO removal_requests"
                " (media_item_id, candidate_id, category, reason, state, marked_at,"
                " marked_by, grace_days, due_at, collection_added, updated_at,"
                " reason_parts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    candidate.media_item_id,
                    candidate_id,
                    primary.value,
                    reason,
                    RemovalState.MARKED.value,
                    marked_at.isoformat(),
                    marked_by,
                    days,
                    due_at.isoformat(),
                    marked_at.isoformat(),
                    reason_parts,
                ),
            )
            request_id = int(cursor.lastrowid)
            # The request is durable first: a Plex membership always has a
            # request that can undo it.
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if self.collections is not None and not self.settings.dry_run:
            self._add_to_collection(request_id, candidate)
        logger.info(
            "removal_marked",
            extra={
                "request_id": request_id,
                "media_item_id": candidate.media_item_id,
                "category": primary.value,
                "grace_days": days,
                "due_at": due_at.isoformat(),
            },
        )
        return self._request_by_id(request_id)

    # -- views for the later screens --------------------------------------

    def open_requests(self) -> list[RemovalRequest]:
        """Every request still awaiting deletion, earliest due first.

        A request whose scope carrier cannot be read is skipped and
        logged, never raised (S16 rule 1): it keeps its state, the next
        scan retries it and the Leaving soon screen shows it as
        unreadable. One damaged row must not break the callers that
        read the whole open set.
        """
        rows = self.conn.execute(
            "SELECT rr.*, c.facts AS scope_facts"
            " FROM removal_requests rr LEFT JOIN candidates c ON c.id = rr.candidate_id"
            " WHERE rr.state NOT IN (?, ?) ORDER BY rr.due_at, rr.id",
            (RemovalState.CANCELLED.value, RemovalState.DELETED.value),
        ).fetchall()
        requests: list[RemovalRequest] = []
        for row in rows:
            try:
                requests.append(self._to_request(row))
            except LifecycleError as error:
                logger.warning(
                    "removal_request_unreadable",
                    extra={
                        "request_id": int(row["id"]),
                        "error": type(error).__name__,
                    },
                )
        return requests

    def request_for(self, candidate: Candidate) -> RemovalRequest | None:
        """The open request covering exactly this candidate's scope."""
        return self._open_scope_request(
            candidate.media_item_id, candidate.unit, candidate.seasons
        )

    # -- undo and Keep it (S5 steps 2 and 4) -------------------------------

    def undo(self, request: RemovalRequest) -> RemovalRequest:
        """The owner takes the marking back; the item returns to the
        candidates and leaves the Leaving soon collection."""
        return self._cancel_with(
            self._assert_cancelable(request), CancelCode.UNDO
        ).request

    def keep_it(self, request: RemovalRequest) -> RemovalRequest:
        """The owner presses Keep it; same return to the candidates, with
        its own reason."""
        return self._cancel_with(
            self._assert_cancelable(request), CancelCode.KEEP_IT
        ).request

    def _assert_cancelable(self, request: RemovalRequest) -> RemovalRequest:
        """Reload the request and refuse one whose fate is already sealed."""
        current = self._request_by_id(request.id)
        if current.state not in self.CANCELABLE_STATES:
            raise RequestStateError(
                f"removal request {request.id} is {current.state.value};"
                " it can no longer be cancelled"
            )
        return current

    # -- the final re-check (S5 step 4) ------------------------------------

    def run_recheck(self) -> RecheckReport:
        """Close proven-gone scopes, then re-check requests whose grace is over.

        A complete Plex snapshot's absence evidence closes a cancelable
        request as ``already_gone`` even before its due date. It records a
        cancellation, never a verified file deletion. A scope without stored
        evidence is unknown, not gone. Dry runs only report the cancellation.

        The three remaining exits apply in order: the item no longer matches its
        category, it became protected, or any non-excluded user played
        any part of its scope after it was marked. A present request whose
        due date has not arrived is not touched at all. No exit means the
        pass-through state: the re-check passed and the item awaits
        deletion (T7), staying in the collection until then.
        """
        now = _utc(self.clock())
        outcomes: list[RecheckOutcome] = []
        errors: list[str] = []
        # An earlier failed collection write may still owe the collection a
        # member or a removal; this run finishes that job first.
        self.retry_collection_additions()
        self.retry_collection_cleanup()
        # A cancellation claim a crash left behind goes back to the failure
        # state first, so this very run re-checks it.
        self.recover_interrupted_cancellations()
        rows = self.conn.execute(
            "SELECT id FROM removal_requests WHERE state IN (?, ?, ?) ORDER BY id",
            (RemovalState.MARKED.value, RemovalState.REMOVAL_FAILED.value,
             RemovalState.RECHECK_PASSED.value),
        ).fetchall()
        for row in rows:
            try:
                request = self._request_by_id(int(row["id"]))
                # Plex absence closes a request even during its grace period.
                # This is cancellation, not verified on-disk deletion.
                if self._scope_is_gone(request.media_item_id, request.unit, request.seasons):
                    outcomes.append(self._cancel_with(request, CancelCode.ALREADY_GONE))
                    continue
                if request.due_at > now:
                    continue
                outcomes.append(self._recheck_one(request, now))
            except Exception as error:
                # One failing request must not starve the others. Report it so
                # orchestration cannot mistake a partial check for success.
                diagnostic = type(error).__name__
                errors.append(diagnostic)
                logger.warning(
                    "removal_recheck_failed",
                    extra={"request_id": int(row["id"]), "error": diagnostic},
                )
        logger.info(
            "removal_recheck_finished",
            extra={
                "checked": len(outcomes),
                "cancelled": len(outcomes) - len([o for o in outcomes if o.passed]),
                "passed": len([o for o in outcomes if o.passed]),
            },
        )
        return RecheckReport(outcomes=tuple(outcomes), errors=tuple(errors))

    def _scope_is_gone(self, media_item_id: int, unit: Unit, seasons: tuple[int, ...]) -> bool:
        """Only explicit snapshot evidence proves absence; no rows means unknown."""
        item = self.conn.execute(
            "SELECT plex_rating_key, plex_present FROM media_items WHERE id = ?",
            (media_item_id,),
        ).fetchone()
        if item is None:
            return False
        if not item["plex_present"]:
            return True
        if unit is not Unit.SEASONS or not seasons:
            return False
        season_rows = self.conn.execute(
            "SELECT season_number, plex_present FROM media_items"
            " WHERE parent_rating_key = ? AND item_type = 'season'",
            (item["plex_rating_key"],),
        ).fetchall()
        episode_rows = self.conn.execute(
            "SELECT season_number, plex_present FROM episodes WHERE series_rating_key = ?",
            (item["plex_rating_key"],),
        ).fetchall()
        for number in seasons:
            containers = [row for row in season_rows if row["season_number"] == number]
            episodes = [row for row in episode_rows if row["season_number"] == number]
            # A present container still exists even when all its episodes are gone.
            if any(row["plex_present"] for row in containers):
                return False
            # A live child contradicts an old container's absence. Never
            # close a scope on contradictory observations from partial reads.
            if any(row["plex_present"] for row in episodes):
                return False
            if containers and not any(row["plex_present"] for row in containers):
                continue
            if episodes and not any(row["plex_present"] for row in episodes):
                continue
            return False
        return True

    def _recheck_one(self, request: RemovalRequest, now: datetime) -> RecheckOutcome:
        candidate = self._candidate_of(request)
        # S14 rule 6: nothing may be deleted outside the curated scope. The
        # scope gates the re-check first, before any other exit applies.
        if not library_scope.item_is_curated(
            self.conn, self.settings, request.media_item_id
        ):
            return self._cancel_with(request, CancelCode.NOT_CURATED)
        # Exit order per S5 step 4.
        if self.rematcher is not None and not self.rematcher.still_matches(candidate):
            return self._cancel_with(request, CancelCode.NO_LONGER_MATCHES)
        if (
            self.protection is not None
            and self.protection.protection_for(candidate) is not None
        ):
            return self._cancel_with(request, CancelCode.PROTECTED)
        play = self._first_play_after(request)
        if play is not None:
            user_name, played_at = play
            # The parameters stay raw (an ISO date): every screen renders
            # them in the language the reader asks for (#47).
            return self._cancel_with(
                request,
                CancelCode.STARTED_WATCHING,
                {"name": user_name, "date": played_at.isoformat()},
            )
        return self._pass(request, now)

    def _cancel_with(
        self,
        request: RemovalRequest,
        code: CancelCode,
        params: Mapping[str, object] | None = None,
    ) -> RecheckOutcome:
        """Cancel the request with a localized reason, take the item out
        of the collection and record the cancellation.

        Every cancellation first reverts the arr side effects a failed
        removal left applied (#69). The request is claimed before a single
        arr request (#99): the state and the effects record are read live,
        under the write lock that takes the claim (#100), so a request
        whose state moved underneath the engine is never touched and the
        revert undoes exactly what the record names at that moment. A
        revert that cannot finish returns the request to the state it came
        from, with a truthful record of what is still applied.
        """
        params = dict(params or {})
        reason = cancel_reason_text(self.locale, code.value, params)
        if self.settings.dry_run and code not in (CancelCode.UNDO, CancelCode.KEEP_IT):
            return RecheckOutcome(request, False, code, reason)
        try:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            state, effects = self._live_cancelable_record(request.id)
            if effects:
                if self.reverter is None:
                    # No arr to talk to: nothing can be reverted, and nothing
                    # is claimed. The record already holds every effect.
                    raise SideEffectRevertError(
                        effects, "no reverter is configured to undo the applied effects"
                    )
                self._move_state(request.id, state, RemovalState.CANCELLING)
                # The claim is durable before the first arr request.
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if effects:
            self._revert_applied_effects(request.id, state, effects)
            state = RemovalState.CANCELLING
        now = _utc(self.clock())
        try:
            # A compare-and-set: a request whose state moved underneath
            # the engine is never overwritten.
            cursor = self.conn.execute(
                "UPDATE removal_requests SET state = ?, cancel_reason = ?,"
                " recheck_status = ?, cancel_params = ?, applied_effects = NULL,"
                " updated_at = ? WHERE id = ? AND state = ?",
                (
                    RemovalState.CANCELLED.value,
                    reason,
                    code.value,
                    json.dumps(params),
                    now.isoformat(),
                    request.id,
                    state.value,
                ),
            )
            if cursor.rowcount == 0:
                raise RequestStateError(
                    f"removal request {request.id} changed state and can no longer"
                    " be cancelled"
                )
            self._record_cancellation(request, reason, now, code.value, params)
            # The cancellation is durable first: an open request never
            # loses its membership silently.
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if self.cleanup_backlog_on_cancel:
            self.retry_collection_cleanup()
        else:
            self.retry_collection_cleanup(request_id=request.id)
        logger.info(
            "removal_cancelled",
            extra={"request_id": request.id, "exit": code.value},
        )
        return RecheckOutcome(
            request=self._request_by_id(request.id),
            passed=False,
            exit_code=code,
            cancel_reason=reason,
        )

    def _live_cancelable_record(
        self, request_id: int
    ) -> tuple[RemovalState, tuple[AppliedEffect, ...]]:
        """The request's state and effects record as stored right now.

        A request that is no longer cancelable lost the claim: it is
        reported and its record is left exactly as it is (#99).
        """
        row = self.conn.execute(
            "SELECT state, applied_effects FROM removal_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise RequestStateError(f"removal request {request_id} no longer exists")
        try:
            state = RemovalState(row["state"])
        except ValueError as error:
            raise LifecycleError(
                f"removal request {request_id} has unreadable data: {error}"
            ) from None
        if state not in self.CANCELABLE_STATES:
            raise RequestStateError(
                f"removal request {request_id} changed state and can no longer"
                " be cancelled"
            )
        try:
            effects = applied_effects_from_json(row["applied_effects"])
        except ValueError as error:
            # An unreadable record is refused closed (#69): the cancel can
            # neither trust it nor touch the arr on its word.
            raise LifecycleError(
                f"removal request {request_id} has an unreadable"
                f" applied-effects record: {error}"
            ) from None
        return state, effects

    def _move_state(
        self, request_id: int, current: RemovalState, target: RemovalState
    ) -> None:
        """A compare-and-set of the state alone; a lost race raises."""
        cursor = self.conn.execute(
            "UPDATE removal_requests SET state = ?, updated_at = ?"
            " WHERE id = ? AND state = ?",
            (target.value, _utc(self.clock()).isoformat(), request_id, current.value),
        )
        if cursor.rowcount == 0:
            raise RequestStateError(
                f"removal request {request_id} changed state and can no longer"
                " be cancelled"
            )

    def recover_interrupted_cancellations(self) -> int:
        """Return a cancellation claim a crash left behind to the failure state.

        A claim older than ``STALE_CANCELLATION_CLAIM`` belongs to a revert
        that never finished (#99). Its record still names every effect that
        may be applied, and reverting one twice is harmless, so the request
        goes back to ``REMOVAL_FAILED``: it must pass the final check again
        before a retry, and the owner can cancel it again.
        """
        now = _utc(self.clock())
        cutoff = now - STALE_CANCELLATION_CLAIM
        recovered = 0
        try:
            # The web writes UTC timestamps and the scan its local time, so
            # the age is a parsed time difference, never a string order.
            # An unreadable timestamp proves no live revert: it is stale.
            rows = self.conn.execute(
                "SELECT id, updated_at FROM removal_requests WHERE state = ?",
                (RemovalState.CANCELLING.value,),
            ).fetchall()
            for claim in rows:
                claimed_at = _parse_iso(claim["updated_at"])
                if claimed_at is not None and claimed_at >= cutoff:
                    continue
                # A compare-and-set on the exact claim read: a claim renewed
                # or finished meanwhile is left alone.
                cursor = self.conn.execute(
                    "UPDATE removal_requests SET state = ?, updated_at = ?"
                    " WHERE id = ? AND state = ? AND updated_at IS ?",
                    (
                        RemovalState.REMOVAL_FAILED.value,
                        now.isoformat(),
                        claim["id"],
                        RemovalState.CANCELLING.value,
                        claim["updated_at"],
                    ),
                )
                recovered += cursor.rowcount
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if recovered:
            logger.warning(
                "removal_cancellation_recovered", extra={"requests": recovered}
            )
        return recovered

    def _pass(self, request: RemovalRequest, now: datetime) -> RecheckOutcome:
        """The pass-through state: re-check passed, awaiting deletion."""
        if self.settings.dry_run:
            return RecheckOutcome(request, True, None, None)
        try:
            # A compare-and-set too: a request cancelled meanwhile stays
            # cancelled.
            cursor = self.conn.execute(
                "UPDATE removal_requests SET state = ?, recheck_status = 'passed',"
                " updated_at = ? WHERE id = ? AND state IN (?, ?, ?)",
                (
                    RemovalState.RECHECK_PASSED.value,
                    now.isoformat(),
                    request.id,
                    RemovalState.MARKED.value,
                    RemovalState.REMOVAL_FAILED.value,
                    RemovalState.RECHECK_PASSED.value,
                ),
            )
            if cursor.rowcount == 0:
                raise RequestStateError(
                    f"removal request {request.id} changed state during the re-check"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        logger.info(
            "removal_recheck_passed", extra={"request_id": request.id}
        )
        return RecheckOutcome(
            request=self._request_by_id(request.id),
            passed=True,
            exit_code=None,
            cancel_reason=None,
        )

    # -- private helpers ---------------------------------------------------

    def _revert_applied_effects(
        self,
        request_id: int,
        previous: RemovalState,
        effects: tuple[AppliedEffect, ...],
    ) -> None:
        """Undo the arr side effects a claimed cancellation read (#69, #100).

        Every effect is reverted, and one that cannot finish returns the
        request to the state it came from — no cancellation event — with
        the record trimmed to the effects that are really still applied.
        That is committed before the error is raised, so no caller's
        rollback can hide the liability it names.
        """
        still_applied: list[AppliedEffect] = []
        try:
            for effect in effects:
                try:
                    self.reverter.revert(effect)
                except SideEffectRevertError as error:
                    # Keep going: a later effect can still be reverted, and
                    # only the ones that really are applied stay recorded.
                    still_applied.extend(error.effects)
        except Exception:
            # An unexpected failure proves nothing about what was reverted:
            # the claim is released with the record as it was read.
            self._release_cancellation_claim(request_id, previous, effects)
            raise
        if still_applied:
            record = tuple(still_applied)
            self._release_cancellation_claim(request_id, previous, record)
            raise SideEffectRevertError(
                record,
                f"removal request {request_id} keeps"
                f" {len(record)} applied arr side effect(s)",
            )

    def _release_cancellation_claim(
        self,
        request_id: int,
        previous: RemovalState,
        effects: tuple[AppliedEffect, ...],
    ) -> None:
        """Give a claimed request back to its state with what is still applied."""
        try:
            self.conn.execute(
                "UPDATE removal_requests SET state = ?, applied_effects = ?,"
                " updated_at = ? WHERE id = ? AND state = ?",
                (
                    previous.value,
                    applied_effects_to_json(effects),
                    _utc(self.clock()).isoformat(),
                    request_id,
                    RemovalState.CANCELLING.value,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _record_cancellation(
        self,
        request: RemovalRequest,
        reason: str,
        now: datetime,
        code: str,
        params: Mapping[str, object],
    ) -> None:
        """History keeps every cancellation with its reason (S5 step 4).

        The recorded reason is the cancellation's own: its key plus the
        raw parameters just stored, so History renders the same sentence
        in the reader's language (#47, S1c).
        """
        reason_parts = json.dumps([text.reason_part(f"removal.cancel.{code}", **params)])
        self.conn.execute(
            "INSERT INTO removal_events"
            " (removal_request_id, media_item_id, outcome, title, unit, category,"
            " reason, marked_by, occurred_at, reason_parts)"
            " VALUES (?, ?, 'cancelled', ?, ?, ?, ?, ?, ?, ?)",
            (
                request.id,
                request.media_item_id,
                self._history_title(request),
                request.unit.value,
                request.category.value,
                reason,
                request.marked_by,
                now.isoformat(),
                reason_parts,
            ),
        )

    def _history_title(self, request: RemovalRequest) -> str:
        row = self.conn.execute(
            "SELECT title FROM media_items WHERE id = ?", (request.media_item_id,)
        ).fetchone()
        title = row["title"] if row is not None else str(request.media_item_id)
        if request.unit is Unit.SEASONS and request.seasons:
            return f"{title} {text.season_runs(list(request.seasons))}"
        return title

    def _candidate_of(self, request: RemovalRequest) -> Candidate:
        """Rebuild the marked candidate from the stored scope carrier.

        The re-check asks the injected ports with the same shape the
        marking used; the reasons are not re-rendered here because no
        port needs them.
        """
        row = self.conn.execute(
            "SELECT plex_rating_key, title, rating FROM media_items WHERE id = ?",
            (request.media_item_id,),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                f"media item {request.media_item_id} of request {request.id}"
                " is not in the synced database"
            )
        return Candidate(
            media_item_id=request.media_item_id,
            plex_rating_key=row["plex_rating_key"],
            title=row["title"],
            unit=request.unit,
            seasons=request.seasons,
            matches=tuple(
                Match(category, "") for category in self._stored_categories(request)
            ),
            size_bytes=0,
            rating=row["rating"],
            facts={},
        )

    def _stored_categories(self, request: RemovalRequest) -> tuple[Category, ...]:
        """The categories the marked candidate matched, primary first."""
        others: list[Category] = []
        if request.candidate_id is not None:
            row = self.conn.execute(
                "SELECT facts FROM candidates WHERE id = ?",
                (request.candidate_id,),
            ).fetchone()
            if row is not None and row["facts"]:
                try:
                    data = json.loads(row["facts"])
                except ValueError:
                    data = {}
                for name in (data.get("matches") or {}):
                    try:
                        category = Category(name)
                    except ValueError:
                        continue
                    if category is not request.category:
                        others.append(category)
        return (request.category, *others)

    def _first_play_after(
        self, request: RemovalRequest
    ) -> tuple[str, datetime] | None:
        """The earliest play of the scope after marking, if any.

        "Any part of it" is the scope's own rating keys; excluded users
        (by flag or by settings name, as detection counts them) never
        count.
        """
        keys = self._scope_keys(request)
        if not keys:
            return None
        excluded_names = {
            name.strip().lower()
            for name in self.settings.excluded_users
            if name.strip()
        }
        placeholders = ", ".join("?" for _ in keys)
        best: tuple[datetime, str] | None = None
        for row in self.conn.execute(
            f"SELECT pe.played_at, u.title, u.excluded FROM play_events pe"
            f" JOIN users u ON u.id = pe.user_id"
            f" WHERE pe.plex_rating_key IN ({placeholders})",
            tuple(keys),
        ):
            if row["excluded"]:
                continue
            if (row["title"] or "").strip().lower() in excluded_names:
                continue
            played_at = _parse_iso(row["played_at"])
            if played_at is None or played_at <= request.marked_at:
                continue
            if best is None or played_at < best[0]:
                best = (played_at, row["title"])
        if best is None:
            return None
        return best[1], best[0]

    def _scope_keys(self, request: RemovalRequest) -> set[str]:
        """The Plex rating keys a play of the scope would carry.

        This is the *watch* scope, not the deletion scope: the series key
        belongs here, because a history entry recorded against the show
        is a play of the season too, and the final check must never miss
        it (S5 step 4). The deletion scope is a different set
        (``_scope_collection_keys`` and the executor's file collection),
        and it never falls back to the series (S16 rule 2).
        """
        if request.unit not in (Unit.MOVIE, Unit.SERIES, Unit.SEASONS):
            # #133: an unknown unit is never checked as a series; the final
            # check fails for it, so it can never pass to the executor.
            raise ValueError(f"unsupported removal unit: {request.unit!r}")
        rating_key = self._rating_key(request.media_item_id)
        if request.unit is Unit.MOVIE:
            return {rating_key}
        wanted = set(request.seasons)
        keys = {rating_key}
        for row in self.conn.execute(
            "SELECT plex_rating_key, season_number FROM episodes"
            " WHERE series_rating_key = ?",
            (rating_key,),
        ):
            if request.unit is Unit.SEASONS and row["season_number"] not in wanted:
                continue
            keys.add(row["plex_rating_key"])
        return keys

    def _has_unreadable_open_request(self, media_item_id: int) -> bool:
        """An unknown scope still reserves its item, but not other items.

        Bulk readers skip unreadable requests; marking must instead fail
        closed because the old scope cannot be proven disjoint from a new
        one. This guard never repairs or mutates the damaged request.
        """
        rows = self.conn.execute(
            "SELECT rr.*, c.facts AS scope_facts FROM removal_requests rr"
            " LEFT JOIN candidates c ON c.id = rr.candidate_id"
            " WHERE rr.media_item_id = ? AND rr.state NOT IN (?, ?)",
            (media_item_id, RemovalState.CANCELLED.value, RemovalState.DELETED.value),
        )
        for row in rows:
            try:
                self._to_request(row)
            except LifecycleError:
                return True
        return False

    def _open_scope_request(
        self, media_item_id: int, unit: Unit, seasons: tuple[int, ...]
    ) -> RemovalRequest | None:
        wanted = set(seasons)
        for request in self.open_requests():
            if (
                request.media_item_id == media_item_id
                and request.unit == unit
                and set(request.seasons) == wanted
            ):
                return request
        return None

    def _add_to_collection(
        self, request_id: int, candidate: Candidate
    ) -> None:
        """S5 step 3: put the marked scope in the Leaving soon collection."""
        section_key = self._section_key(candidate.media_item_id)
        collection_key = self.collections.ensure_collection(
            section_key, self.settings.collection_name
        )
        scope_keys = self._scope_collection_keys(
            candidate.media_item_id, candidate.unit, candidate.seasons
        )
        for key in sorted(scope_keys):
            self.collections.add_to_collection(collection_key, key)
            # Every member is recorded as it is added, so a failure later
            # in the scope leaves the earlier members tracked, not stranded.
            self._record_members(request_id, [key])

    def _record_members(
        self, request_id: int, keys: list[str], *, added: bool = True
    ) -> None:
        """Track the collection members this request owns (S5 step 3).

        They live in the request's scope carrier, the ``facts`` of its
        ``candidates`` row, so no schema change is needed.
        """
        row = self.conn.execute(
            "SELECT rr.candidate_id, c.facts FROM removal_requests rr"
            " LEFT JOIN candidates c ON c.id = rr.candidate_id WHERE rr.id = ?",
            (request_id,),
        ).fetchone()
        data = json.loads(row["facts"] or "{}")
        members = list(data.get("collection_members") or [])
        for key in keys:
            if added and key not in members:
                members.append(key)
            elif not added:
                members = [member for member in members if member != key]
        data["collection_members"] = members
        self.conn.execute(
            "UPDATE candidates SET facts = ? WHERE id = ?",
            (json.dumps(data), row["candidate_id"]),
        )
        self.conn.execute(
            "UPDATE removal_requests SET collection_added = ? WHERE id = ?",
            (1 if members else 0, request_id),
        )
        self.conn.commit()

    def retry_collection_additions(self) -> int:
        """Add the members a failed collection write left out (S5 step 3).

        Every marked request whose scope is not wholly recorded in the
        collection gets its missing members added; a failure keeps the
        rest for the next retry. The number of requests made whole is
        returned.
        """
        if self.collections is None or self.settings.dry_run:
            return 0
        finished = 0
        for row in self.conn.execute(
            "SELECT id FROM removal_requests WHERE state IN (?, ?, ?, ?) ORDER BY id",
            (RemovalState.MARKED.value, RemovalState.RECHECK_PASSED.value,
             RemovalState.REMOVAL_FAILED.value, RemovalState.DELETING.value),
        ).fetchall():
            request_id = int(row["id"])
            try:
                request = self._request_by_id(request_id)
                if self._scope_is_gone(request.media_item_id, request.unit, request.seasons):
                    continue
                missing = self._scope_collection_keys(
                    request.media_item_id, request.unit, request.seasons
                ) - set(request.collection_members)
                if not missing:
                    continue
                collection_key = self.collections.ensure_collection(
                    self._section_key(request.media_item_id),
                    self.settings.collection_name,
                )
                for key in sorted(missing):
                    self.collections.add_to_collection(collection_key, key)
                    self._record_members(request_id, [key])
                finished += 1
            except Exception as error:
                logger.warning(
                    "removal_collection_addition_failed",
                    extra={"request_id": request_id, "error": type(error).__name__},
                )
        return finished

    def retry_collection_cleanup(self, *, request_id: int | None = None) -> int:
        """Finish the collection removals a failed write left behind.

        Every cancelled request that still records collection members is
        cleaned up again; a removal that fails keeps its members recorded
        for the next retry. The number of requests it finished is returned.
        With ``request_id`` (#155) only that request is cleaned up, so a
        web request never walks the whole backlog.
        """
        if self.collections is None or self.settings.dry_run:
            return 0
        needed: set[str] = set()
        for row in self.conn.execute(
            "SELECT c.facts FROM removal_requests rr"
            " LEFT JOIN candidates c ON c.id = rr.candidate_id"
            " WHERE rr.state NOT IN (?, ?)",
            (RemovalState.CANCELLED.value, RemovalState.DELETED.value),
        ):
            needed.update(_recorded_members(row["facts"]))
        finished = 0
        for row in self.conn.execute(
            "SELECT id FROM removal_requests"
            " WHERE state IN (?, ?) AND collection_added = 1"
            " AND (? IS NULL OR id = ?) ORDER BY id",
            (RemovalState.CANCELLED.value, RemovalState.DELETED.value, request_id, request_id),
        ).fetchall():
            row_id = int(row["id"])
            cleared: list[str] = []
            try:
                request = self._request_by_id(row_id)
                if not request.collection_members:
                    continue
                collection_key = self.collections.ensure_collection(
                    self._section_key(request.media_item_id),
                    self.settings.collection_name,
                )
                for key in request.collection_members:
                    if key in needed:
                        cleared.append(key)
                        continue
                    self.collections.remove_from_collection(collection_key, key)
                    cleared.append(key)
                self._record_members(row_id, cleared, added=False)
                if not self._request_by_id(row_id).collection_members:
                    finished += 1
            except Exception:
                logger.warning(
                    "removal_collection_cleanup_failed",
                    extra={"request_id": row_id},
                )
        logger.info("removal_collection_cleanup_finished", extra={"finished": finished})
        return finished

    def _scope_collection_keys(
        self, media_item_id: int, unit: Unit, seasons: tuple[int, ...]
    ) -> set[str]:
        """The collection keys this scope occupies.

        A seasons scope occupies its own season rows, never the series
        parent, so two scopes of one series never share a member key.
        When the season rows are missing the scope occupies nothing at
        all -- it never falls back to the series row (S16 rule 2).
        """
        rating_key = self._rating_key(media_item_id)
        if unit is not Unit.SEASONS:
            return {rating_key}
        rows = self.conn.execute(
            "SELECT plex_rating_key, season_number FROM media_items"
            " WHERE parent_rating_key = ? AND plex_present = 1",
            (rating_key,),
        )
        wanted = set(seasons)
        return {
            row["plex_rating_key"] for row in rows if row["season_number"] in wanted
        }

    def _section_key(self, media_item_id: int) -> str:
        row = self.conn.execute(
            "SELECT l.plex_key FROM media_items mi"
            " JOIN libraries l ON l.id = mi.library_id WHERE mi.id = ?",
            (media_item_id,),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                f"media item {media_item_id} is not in the synced database"
            )
        return row["plex_key"]

    def _request_by_id(self, request_id: int) -> RemovalRequest:
        row = self.conn.execute(
            "SELECT rr.*, c.facts AS scope_facts"
            " FROM removal_requests rr LEFT JOIN candidates c ON c.id = rr.candidate_id"
            " WHERE rr.id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise LifecycleError(f"no removal request with id {request_id}")
        return self._to_request(row)

    def _to_request(self, row: sqlite3.Row) -> RemovalRequest:
        request_id = int(row["id"])
        unit, seasons, members = _parse_scope(row["scope_facts"], request_id)
        marked_at = _parse_iso(row["marked_at"])
        due_at = _parse_iso(row["due_at"])
        if marked_at is None or due_at is None:
            raise LifecycleError(
                f"removal request {request_id} has unreadable marked_at or due_at"
            )
        try:
            category = Category(row["category"])
            state = RemovalState(row["state"])
        except ValueError as error:
            raise LifecycleError(
                f"removal request {request_id} has unreadable data: {error}"
            ) from None
        try:
            applied_effects = applied_effects_from_json(row["applied_effects"])
        except ValueError as error:
            # An unreadable record is refused closed (#69): the cancel can
            # neither trust it nor touch the arr on its word.
            raise LifecycleError(
                f"removal request {request_id} has an unreadable"
                f" applied-effects record: {error}"
            ) from None
        return RemovalRequest(
            id=request_id,
            media_item_id=int(row["media_item_id"]),
            candidate_id=(
                int(row["candidate_id"]) if row["candidate_id"] is not None else None
            ),
            category=category,
            unit=unit,
            seasons=seasons,
            state=state,
            marked_at=marked_at,
            marked_by=row["marked_by"] or "owner",
            grace_days=int(row["grace_days"]),
            due_at=due_at,
            collection_added=bool(row["collection_added"]),
            recheck_status=row["recheck_status"],
            cancel_reason=row["cancel_reason"],
            collection_members=members,
            applied_effects=applied_effects,
            last_error=row["last_error"],
            deleted_at=_parse_iso(row["deleted_at"]),
            cancel_params=_cancel_params(row["cancel_params"]),
        )

    def _rating_key(self, media_item_id: int) -> str:
        row = self.conn.execute(
            "SELECT plex_rating_key FROM media_items WHERE id = ?", (media_item_id,)
        ).fetchone()
        if row is None:
            raise LifecycleError(
                f"media item {media_item_id} is not in the synced database"
            )
        return row["plex_rating_key"]


def _parse_scope(
    facts_text: str | None, request_id: int
) -> tuple[Unit, tuple[int, ...], tuple[str, ...]]:
    """Read the scope carrier of a request: its unit, seasons and members."""
    if not facts_text:
        raise LifecycleError(
            f"removal request {request_id} has no readable scope:"
            " its candidates row is missing"
        )
    try:
        data = json.loads(facts_text)
        unit = Unit(data["unit"])
        seasons = tuple(sorted({int(number) for number in data.get("seasons") or ()}))
    except (ValueError, KeyError, TypeError) as error:
        raise LifecycleError(
            f"removal request {request_id} has a damaged scope carrier: {error}"
        ) from None
    return unit, seasons, _recorded_members(facts_text)


def _recorded_members(facts_text: str | None) -> tuple[str, ...]:
    """The collection members a scope carrier records, if it is readable."""
    try:
        data = json.loads(facts_text or "{}")
    except ValueError:
        return ()
    return tuple(str(key) for key in data.get("collection_members") or ())
