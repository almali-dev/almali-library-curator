"""Typed results of the removal lifecycle (S5).

The lifecycle is a state machine over ``removal_requests``: the owner
marks a candidate, the item sits in the "Leaving soon" Plex collection
for a grace period, and a final re-check decides its fate. The values of
:class:`RemovalState` persist in ``removal_requests.state``; the machine
itself never deletes anything — that is the next stage (T7, S5 step 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from almali_library_curator.detection.models import Category, Unit


class RemovalState(StrEnum):
    """The states a removal request goes through (S5).

    ``MARKED`` is where a fresh request starts, in its grace period.
    ``RECHECK_PASSED`` is the pass-through state: the final re-check
    passed on the last day and the item now awaits deletion, staying in
    the Leaving soon collection until that deletion removes it.
    ``CANCELLED`` means the removal will not happen — the item is back
    among the candidates and out of the collection. ``REMOVAL_FAILED``
    stays open and must pass the final check again before retrying.
    ``DELETING`` is a committed execution claim: cancellation is no longer
    possible. ``DELETED`` is terminal and follows on-disk verification.
    """

    MARKED = "marked"
    RECHECK_PASSED = "recheck_passed"
    DELETING = "deleting"
    CANCELLED = "cancelled"
    REMOVAL_FAILED = "removal_failed"
    DELETED = "deleted"


class CancelCode(StrEnum):
    """Why a removal was cancelled.

    The value persists in ``removal_requests.recheck_status`` and is the
    suffix of the ``removal.cancel.*`` translation key that renders the
    localized ``cancel_reason``.
    """

    UNDO = "undo"
    KEEP_IT = "keep_it"
    NO_LONGER_MATCHES = "no_longer_matches"
    PROTECTED = "protected"
    STARTED_WATCHING = "started_watching"


@dataclass(frozen=True)
class RemovalRequest:
    """One marked item and everything its lifecycle needs.

    ``unit`` and ``seasons`` — the scope — come from the ``candidates``
    row the request was marked from: that row's ``facts`` JSON is the
    scope carrier, and this object is its typed reading.
    """

    id: int
    media_item_id: int
    candidate_id: int | None
    category: Category
    unit: Unit
    seasons: tuple[int, ...]
    state: RemovalState
    marked_at: datetime
    marked_by: str
    grace_days: int
    due_at: datetime
    collection_added: bool
    recheck_status: str | None
    cancel_reason: str | None
    # The collection members this request still owns, read from the
    # ``collection_members`` list of its scope carrier.
    collection_members: tuple[str, ...] = ()
    last_error: str | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class RecheckOutcome:
    """The final re-check verdict for one request (S5 step 4)."""

    request: RemovalRequest
    passed: bool
    exit_code: CancelCode | None
    cancel_reason: str | None


@dataclass(frozen=True)
class RecheckReport:
    """Every request one re-check run examined, in run order."""

    outcomes: tuple[RecheckOutcome, ...]
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> tuple[RecheckOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.passed)

    @property
    def cancelled(self) -> tuple[RecheckOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)


class DeletionStatus(StrEnum):
    DELETED = "deleted"
    FAILED = "failed"
    WOULD_DELETE = "would_delete"
    BLOCKED_BY_KILL_SWITCH = "blocked_by_kill_switch"


@dataclass(frozen=True)
class DeletionOutcome:
    """The checked result (or dry-run plan) for one removal request."""

    request_id: int
    status: DeletionStatus
    # A damaged scope can be isolated and reported without inventing a unit.
    request: RemovalRequest | None = None
    size_bytes: int = 0
    freed_bytes: int = 0
    error: str | None = None
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeletionReport:
    """Deletion attempts in request id order; a failure never ends the batch."""

    outcomes: tuple[DeletionOutcome, ...]


class LifecycleError(RuntimeError):
    """Any refusal or inconsistency in the removal lifecycle."""


class DuplicateMarkError(LifecycleError):
    """The same scope already has an open removal request."""


class GraceRangeError(LifecycleError):
    """The grace period is outside the accepted 3-to-60-day range."""


class ProtectedItemError(LifecycleError):
    """A protected item can never be marked for removal (S4)."""


class RequestStateError(LifecycleError):
    """The request is not in a state that accepts this action."""
