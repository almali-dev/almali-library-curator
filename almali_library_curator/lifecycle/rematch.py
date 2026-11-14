"""The production rematcher (S5 step 4, exit 1).

The final re-check must ask the detection engine again, not trust the
marked snapshot: the synced database may have moved on since the owner
marked the item. This adapter runs :meth:`DetectionEngine.scan_items` for
the marked media item alone — the full scan's answer for that item, with
the same protection, at a fraction of its cost (#5) — and answers whether
the exact scope — media item, unit and season set — still matches, in at
least one of the categories it was marked for. Each request gets its own
fresh pass, so no answer computed before an earlier cancellation in the
same re-check run is ever reused.
"""

from __future__ import annotations

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.models import Candidate, Category


class DetectionRematcher:
    """Answers ``still_matches`` by re-running detection for one item."""

    def __init__(self, engine: DetectionEngine) -> None:
        self.engine = engine

    def still_matches(self, candidate: Candidate) -> bool:
        """True when the scan still finds the marked scope, in one of
        the categories it was marked for."""
        wanted: set[Category] = set(candidate.categories)
        fresh_candidates = self.engine.scan_items([candidate.media_item_id]).candidates
        for fresh in fresh_candidates:
            if fresh.media_item_id != candidate.media_item_id:
                continue
            if fresh.unit is not candidate.unit:
                continue
            if set(fresh.seasons) != set(candidate.seasons):
                continue
            if wanted & set(fresh.categories):
                return True
        return False
