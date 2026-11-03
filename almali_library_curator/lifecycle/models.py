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
    among the candidates and out of the collection.
    """

    MARKED = "marked"
    RECHECK_PASSED = "recheck_passed"
    CANCELLED = "cancelled"


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

    @property
    def passed(self) -> tuple[RecheckOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.passed)

    @property
    def cancelled(self) -> tuple[RecheckOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)


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
