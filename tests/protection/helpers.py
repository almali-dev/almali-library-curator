"""Scenario builders for the protection tests.

Libraries and items are seeded into a real migrated database; candidates
are real value objects over those rows; full-path scenarios sync a fake
Plex server with ``PlexSync`` exactly as the detection tests do.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fake_plex_server import FakePlexDataset, FakePlexServer

from almali_library_curator.config import Settings
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from almali_library_curator.plex import PlexClient, PlexSync

# The clock every scenario is frozen at, far past every threshold.
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ADDED = datetime(2024, 4, 1, 12, 0, tzinfo=timezone.utc)

MOVIE_SIZE = 24 * 1024**3


def ts(year: int, month: int, day: int) -> int:
    """Epoch seconds for a moment, so fixtures carry explicit dates."""
    return int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())


def default_settings(**overrides) -> Settings:
    return Settings(config_dir="/config", **overrides)


def seed_library(
    conn,
    plex_key: str,
    title: str,
    *,
    section_type: str = "movie",
    protected: bool = False,
) -> int:
    cursor = conn.execute(
        "INSERT INTO libraries (plex_key, title, section_type, protected)"
        " VALUES (?, ?, ?, ?)",
        (plex_key, title, section_type, 1 if protected else 0),
    )
    conn.commit()
    return int(cursor.lastrowid)


def seed_item(
    conn,
    library_id: int,
    rating_key: str,
    title: str,
    *,
    item_type: str = "movie",
    rating: float | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type,"
        " added_at, rating, file_size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (library_id, rating_key, title, item_type, ADDED.isoformat(), rating, MOVIE_SIZE),
    )
    conn.commit()
    return int(cursor.lastrowid)


def make_candidate(
    item_id: int,
    rating_key: str,
    title: str,
    *,
    unit: Unit = Unit.MOVIE,
    seasons: tuple[int, ...] = (),
    rating: float | None = None,
    category: Category = Category.NEVER_WATCHED,
) -> Candidate:
    """A real candidate over a real seeded row, with one matching reason."""
    return Candidate(
        media_item_id=item_id,
        plex_rating_key=rating_key,
        title=title,
        unit=unit,
        seasons=seasons,
        matches=(Match(category, "Added April 2024. 0 plays by any of the 2 users."),),
        size_bytes=MOVIE_SIZE,
        rating=rating,
        facts={},
    )


def sync_dataset(conn, dataset: FakePlexDataset, *, rating_source: str = "critic") -> None:
    with FakePlexServer(dataset) as server:
        client = PlexClient(
            base_url=server.base_url,
            token="test-token",
            page_size=100,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        PlexSync(client, conn, rating_source=rating_source).run()
