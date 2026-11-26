"""Typed results of the detection engine (S3).

A candidate is one item that matched at least one category. Items matching
several categories appear once, carrying every matching reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from almali_library_curator.detection.text import ReasonPart


class Category(StrEnum):
    """The six fixed detection categories, in S3 table order."""

    STALLED_SERIES = "stalled_series"
    NEVER_WATCHED = "never_watched"
    CANCELLED_NO_VIEWERS = "cancelled_no_viewers"
    SEASONS_EVERYONE_FINISHED = "seasons_everyone_finished"
    RUNNING_SERIES_FINISHED_SEASONS = "running_series_finished_seasons"
    MOVIES_NOT_REWATCHED = "movies_not_rewatched"


class Unit(StrEnum):
    """What a candidate applies to: a whole series, some seasons, or a movie."""

    SERIES = "series"
    SEASONS = "seasons"
    MOVIE = "movie"


@dataclass(frozen=True)
class Match:
    """One category that matched, with its one-sentence reason.

    ``reason`` is the sentence rendered in the scan's locale, kept so older
    rows and downgrades still read; ``reason_parts`` is the same sentence
    as a key plus raw parameters, which is what a request renders in its
    own language (#47).
    """

    category: Category
    reason: str
    reason_parts: tuple[ReasonPart, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """An item the scan proposes, with every fact that made it match."""

    media_item_id: int
    plex_rating_key: str
    title: str
    unit: Unit
    seasons: tuple[int, ...]
    matches: tuple[Match, ...]
    size_bytes: int
    rating: float | None
    facts: Mapping[str, object]

    @property
    def categories(self) -> tuple[Category, ...]:
        return tuple(match.category for match in self.matches)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(match.reason for match in self.matches)

    @property
    def reason_parts(self) -> tuple[ReasonPart, ...]:
        """Every matching reason as a key plus raw parameters, in order."""
        return tuple(part for match in self.matches for part in match.reason_parts)
