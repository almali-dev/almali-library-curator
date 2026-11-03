"""Scenario builders for the detection tests.

Each builder paints one S3 scenario on a ``FakePlexDataset``, runs the sync
into a real database and returns the candidates of a scan with a frozen
clock, so thresholds are deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from fake_plex_server import (
    FakePlexDataset,
    FakePlexServer,
    episode,
    history_entry,
    movie,
    season,
    series,
)

from almali_library_curator.config import Settings, Thresholds
from almali_library_curator.db import init_db
from almali_library_curator.plex import PlexClient, PlexSync

GB = 1024**3
EPISODE_MS = 2_700_000

# The clock every scenario is frozen at.
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

MOVIE_SIZE = 24 * GB


def ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> int:
    """Epoch seconds for a moment, so fixtures carry explicit dates."""
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def full_play(key: str, account_id: int, when: int) -> dict:
    """A play event that reaches the end of the item."""
    return history_entry(key, account_id, when, EPISODE_MS, EPISODE_MS)


def partial_play(key: str, account_id: int, when: int, ratio: float) -> dict:
    return history_entry(key, account_id, when, int(EPISODE_MS * ratio), EPISODE_MS)


def add_series(
    dataset: FakePlexDataset,
    rating_key: str,
    title: str,
    seasons: dict[int, list[int]],
    **extra,
) -> None:
    """Add a series whose seasons map to episode sizes in GiB."""
    dataset.items["2"].append(series(rating_key, title, **extra))
    season_items = []
    for number, sizes in seasons.items():
        season_key = f"{rating_key}-{number}"
        season_items.append(season(season_key, rating_key, number))
        episodes = [
            episode(
                f"{season_key}-{index}",
                rating_key,
                season_key,
                index,
                Part=[{"file": f"/media/TV/{title}/{number}-{index}.mkv", "size": size * GB}],
            )
            for index, size in enumerate(sizes, start=1)
        ]
        dataset.children[season_key] = episodes
    dataset.children[rating_key] = season_items


def default_settings(**overrides) -> Settings:
    return Settings(config_dir="/config", **overrides)


@dataclass
class FixedSeriesStatus:
    """A series-status enricher answering one fixed payload."""

    status: str = "unknown"
    ended_year: int | None = None
    rating: float | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def series_status(self, title: str, plex_rating_key: str):
        from almali_library_curator.detection.enrichers import SeriesStatus, SeriesStatusInfo

        self.calls.append((title, plex_rating_key))
        return SeriesStatusInfo(
            status=SeriesStatus(self.status),
            ended_year=self.ended_year,
            rating=self.rating,
        )


@dataclass
class FixedRating:
    """A rating enricher answering one fixed rating."""

    value: float | None = None
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def rating(self, title: str, item_type: str, plex_rating_key: str) -> float | None:
        self.calls.append((title, item_type, plex_rating_key))
        return self.value


def insert_event(
    conn,
    user_title: str,
    rating_key: str,
    played_at: datetime,
    position_ms: int,
    duration_ms: int = EPISODE_MS,
    watched: int = 0,
) -> None:
    """Add a play event directly, for facts history cannot express yet.

    The synced schema carries the watched flag and the position separately,
    so enrichment sources (Tautulli) may deliver one without the other.
    """
    row = conn.execute("SELECT id FROM users WHERE title = ?", (user_title,)).fetchone()
    conn.execute(
        "INSERT INTO play_events"
        " (user_id, plex_rating_key, watched, played_at, position_ms, duration_ms, source)"
        " VALUES (?, ?, ?, ?, ?, ?, 'tautulli')",
        (
            int(row["id"]),
            rating_key,
            watched,
            played_at.isoformat(),
            position_ms,
            duration_ms,
        ),
    )
    conn.commit()


def sync_dataset(conn, dataset: FakePlexDataset) -> None:
    with FakePlexServer(dataset) as server:
        client = PlexClient(
            base_url=server.base_url,
            token="test-token",
            page_size=100,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        PlexSync(client, conn).run()


def scan(
    conn,
    *,
    settings: Settings | None = None,
    series_status=None,
    rating=None,
    now: datetime = NOW,
):
    """Run the engine over the synced database with a frozen clock."""
    from almali_library_curator.detection.engine import DetectionEngine

    enrichers = None
    if series_status is not None or rating is not None:
        from almali_library_curator.detection.enrichers import Enrichers

        enrichers = Enrichers(series_status=series_status, rating=rating)
    engine = DetectionEngine(
        conn,
        enrichers=enrichers,
        clock=lambda: now,
        settings=settings or default_settings(),
    )
    return engine.scan()


def with_thresholds(settings: Settings, **months: int) -> Settings:
    return replace(settings, thresholds=replace(Thresholds(), **months))


def candidates_for(candidates, title: str):
    return [candidate for candidate in candidates if title in candidate.title]
