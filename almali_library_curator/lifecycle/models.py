"""Typed results of the removal lifecycle (S5).

The lifecycle is a state machine over ``removal_requests``: the owner
marks a candidate, the item sits in the "Leaving soon" Plex collection
for a grace period, and a final re-check decides its fate. The values of
:class:`RemovalState` persist in ``removal_requests.state``; the machine
itself never deletes anything — that is the next stage (T7, S5 step 5).
"""

from __future__ import annotations

import json
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
    ``CANCELLING`` is a committed cancellation claim (#99): it is taken
    before the arr side effects of a failed removal are reverted, so no
    deletion can claim the request meanwhile. It ends as ``CANCELLED``,
    or returns to the state it came from when a revert cannot finish; a
    claim left behind by a crash returns to ``REMOVAL_FAILED``.
    """

    MARKED = "marked"
    RECHECK_PASSED = "recheck_passed"
    DELETING = "deleting"
    CANCELLING = "cancelling"
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
    NOT_CURATED = "not_curated"
    ALREADY_GONE = "already_gone"


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
    # The arr side effects this request still holds applied (#69), read
    # from ``removal_requests.applied_effects``.
    applied_effects: tuple["AppliedEffect", ...] = ()
    last_error: str | None = None
    deleted_at: datetime | None = None
    # The raw parameters of the cancellation reason, read from
    # ``removal_requests.cancel_params``: a key plus what was observed
    # (names, ISO dates), rendered per screen (#47). ``None`` is a row
    # written before the parts existed, which keeps its stored text.
    cancel_params: dict[str, object] | None = None


# The kind of side effect a removal applied; the values persist inside
# the ``applied_effects`` JSON record (#69). The arr the effect names is
# validated by shape only: an unknown service has no client at revert
# time and fails closed there.
APPLIED_KINDS = frozenset({"unmonitor", "exclusion"})


@dataclass(frozen=True)
class AppliedEffect:
    """One arr side effect a removal applied and a cancel must undo (#69).

    The record is the truth about what the arr now holds: an ``unmonitor``
    names the resource (and, for a seasons scope, exactly the seasons it
    stopped monitoring), an ``exclusion`` names the import exclusion entry
    it stored. Serialized, it keeps only the fields that apply, so the
    stored JSON reads exactly what was applied.
    """

    kind: str  # "unmonitor" | "exclusion"
    arr: str  # the arr service the effect was applied to
    resource_id: int | None = None
    seasons: tuple[int, ...] = ()
    external_id: int | None = None
    exclusion_id: int | None = None
    title: str | None = None

    def to_dict(self) -> dict:
        """The JSON shape of this effect: only the fields that apply."""
        data: dict = {"kind": self.kind, "arr": self.arr}
        if self.kind == "unmonitor":
            data["resource_id"] = self.resource_id
            if self.seasons:
                data["seasons"] = list(self.seasons)
        else:
            data["external_id"] = self.external_id
            data["exclusion_id"] = self.exclusion_id
            data["title"] = self.title
        return data

    @classmethod
    def from_dict(cls, data: object) -> "AppliedEffect":
        """Parse one effect from its stored JSON shape.

        Raises ``ValueError`` on anything that is not a well-formed
        effect record, so an unreadable row is refused closed.
        """
        if not isinstance(data, dict):
            raise ValueError(f"an applied effect must be an object, got {type(data).__name__}")
        kind = data.get("kind")
        arr = data.get("arr")
        if kind not in APPLIED_KINDS:
            raise ValueError(f"unknown applied-effect kind: {kind!r}")
        if not isinstance(arr, str) or not arr:
            raise ValueError(f"unreadable applied-effect arr: {arr!r}")
        seasons = tuple(data.get("seasons") or ())
        if not all(isinstance(number, int) for number in seasons):
            raise ValueError(f"unreadable applied-effect seasons: {seasons!r}")
        return cls(
            kind=kind,
            arr=arr,
            resource_id=data.get("resource_id"),
            seasons=seasons,
            external_id=data.get("external_id"),
            exclusion_id=data.get("exclusion_id"),
            title=data.get("title"),
        )


def applied_effects_to_json(effects: tuple["AppliedEffect", ...]) -> str:
    """The ``removal_requests.applied_effects`` value for these effects."""
    return json.dumps([effect.to_dict() for effect in effects])


def applied_effects_from_json(text: str | None) -> tuple["AppliedEffect", ...]:
    """Parse the ``applied_effects`` record; ``None`` means none applied.

    Raises ``ValueError`` on anything that is not a JSON list of
    well-formed effects, so the caller can refuse closed (#69).
    """
    if not text:
        return ()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("the applied-effects record must be a JSON list")
    return tuple(AppliedEffect.from_dict(entry) for entry in data)


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


class UncuratedItemError(LifecycleError):
    """The item's library is outside the curated scope (S14 rule 6)."""


class RequestStateError(LifecycleError):
    """The request is not in a state that accepts this action."""


class SideEffectRevertError(LifecycleError):
    """An arr side effect a cancellation could not undo (#69).

    ``effects`` carries the effects that are still applied — the ones
    that really were reverted are already dropped — so the caller can
    persist exactly that record and report the liability.
    """

    def __init__(self, effects, message: str | None = None) -> None:
        self.effects = tuple(effects)
        super().__init__(
            message
            or f"{len(self.effects)} applied arr side effect(s) could not be reverted"
        )
