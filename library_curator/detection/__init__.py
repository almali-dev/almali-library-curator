"""Detection engine (S3): the five categories over the synced database."""

from library_curator.detection.engine import DetectionEngine
from library_curator.detection.enrichers import (
    Enrichers,
    RatingEnricher,
    SeriesStatus,
    SeriesStatusEnricher,
    SeriesStatusInfo,
)
from library_curator.detection.models import Candidate, Category, Match, Unit

__all__ = [
    "Candidate",
    "Category",
    "DetectionEngine",
    "Enrichers",
    "Match",
    "RatingEnricher",
    "SeriesStatus",
    "SeriesStatusEnricher",
    "SeriesStatusInfo",
    "Unit",
]
