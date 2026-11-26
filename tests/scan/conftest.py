"""Reuse HTTP boundary fixtures, never the parallel enrichment package."""
import sys
from pathlib import Path

for name in ("plex", "radarr", "sonarr", "tautulli", "lifecycle", "tmdb"):
    directory = str(Path(__file__).resolve().parent.parent / name)
    if directory not in sys.path:
        sys.path.insert(0, directory)
