"""Typed results of the protection engine (S4).

A protection is what keeps an item out of the candidates: a protected
library, a high rating, or a hand-pressed "Keep forever". The engine only
filters and records protections; it never creates, marks or advances a
removal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from almali_library_curator.detection.models import Candidate
from almali_library_curator.detection.text import ReasonPart


class ProtectionKind(StrEnum):
    """The three S4 protections, with the values the ``protections`` table
    already allows in its kind CHECK."""

    LIBRARY = "library"
    RATING = "rating"
    KEEP_FOREVER = "keep_forever"


@dataclass(frozen=True)
class Protection:
    """What protects one item.

    ``library_id`` carries the protecting library, ``media_item_id`` the
    protected row, and ``detail`` the free-form fact of the protection:
    the library title, the item's rating, or the stored scope of a
    kept-forever row. Each applies where its kind needs it.
    """

    kind: ProtectionKind
    library_id: int | None = None
    media_item_id: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ProtectedCandidate:
    """A candidate with the protection that covers it and the localized
    reason the Protected view shows.

    ``reason`` is the sentence rendered in the scan's locale, kept for
    older rows and downgrades; ``reason_parts`` is the same sentence as a
    key plus raw parameters, rendered in the request's language (#47).

    The category the item would otherwise match stays on the candidate's
    own ``matches``/``categories``.
    """

    candidate: Candidate
    protection: Protection
    reason: str
    reason_parts: tuple[ReasonPart, ...] = ()


@dataclass(frozen=True)
class ProtectionOutcome:
    """One split of candidates: those that survive and those protected."""

    candidates: tuple[Candidate, ...]
    protected: tuple[ProtectedCandidate, ...]
