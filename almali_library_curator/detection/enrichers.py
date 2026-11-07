"""Protocols for the external facts detection needs.

Detection is pure computation: it never talks to the network. Every fact
that comes from outside the synced database arrives through one of these
small protocols, implemented on top of Sonarr and TMDB later (T5, T6, T8).
An absent enricher degrades per S2: the categories that need the fact
simply do not match on that fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SeriesStatus(StrEnum):
    """The production status of a series, as Sonarr or TMDB reports it."""

    CONTINUING = "continuing"
    ENDED = "ended"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SeriesStatusInfo:
    """What an enricher knows about one series.

    ``ended_year`` feeds the "Cancelled in {year}" reason sentence and may
    be unknown. ``rating`` is a rating fallback for when Plex has none.
    """

    status: SeriesStatus = SeriesStatus.UNKNOWN
    ended_year: int | None = None
    rating: float | None = None


class SeriesStatusEnricher(Protocol):
    """Series facts from Sonarr or TMDB.

    Returns ``None`` when the source cannot answer (absent, unreachable);
    an answered :class:`SeriesStatusInfo` with ``unknown`` status means the
    source knows nothing about the series. Both degrade the same way.
    """

    def series_status(self, title: str, plex_rating_key: str) -> SeriesStatusInfo | None: ...


class RatingEnricher(Protocol):
    """A rating from TMDB for an item Plex stores no rating for.

    Returns ``None`` when the source cannot answer.
    """

    def rating(self, title: str, item_type: str, plex_rating_key: str) -> float | None: ...


@dataclass(frozen=True)
class Enrichers:
    """The optional external-fact providers, injected into the engine."""

    series_status: SeriesStatusEnricher | None = None
    rating: RatingEnricher | None = None
