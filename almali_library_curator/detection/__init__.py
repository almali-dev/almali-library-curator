"""Detection engine (S3): the six categories over the synced database."""

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.enrichers import (
    Enrichers,
    RatingEnricher,
    SeriesStatus,
    SeriesStatusEnricher,
    SeriesStatusInfo,
)
from almali_library_curator.detection.models import Candidate, Category, Match, Unit

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
