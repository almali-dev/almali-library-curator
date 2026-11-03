"""Scenario builders for the lifecycle tests.

Libraries, items, episodes, users and play events are seeded into a real
migrated database; candidates are real value objects over those rows;
full-path scenarios sync a fake Plex server with ``PlexSync`` exactly as
the detection and protection tests do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from almali_library_curator.config import Settings
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from fake_plex_server import history_entry

# The clock every scenario is frozen at, far past every threshold.
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ADDED = datetime(2024, 4, 1, 12, 0, tzinfo=timezone.utc)

MOVIE_SIZE = 24 * 1024**3
EPISODE_SIZE = 2 * 1024**3
EPISODE_MS = 2_700_000


def ts(year: int, month: int, day: int) -> int:
    """Epoch seconds for a moment, so fixtures carry explicit dates."""
    return int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())


def full_play(key: str, account_id: int, when: int) -> dict:
    """A play event that reaches the end of the item."""
    return history_entry(key, account_id, when, EPISODE_MS, EPISODE_MS)


def default_settings(**overrides) -> Settings:
    return Settings(config_dir="/config", **overrides)


class FrozenClock:
    """A clock the tests advance by hand."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now = self.now + timedelta(**delta)


@dataclass
class FakeCollections:
    """An in-memory ``CollectionsPort`` recording every call."""

    keys: dict[tuple[str, str], str] = field(default_factory=dict)
    membership: dict[str, set[str]] = field(default_factory=dict)
    ensure_calls: list[tuple[str, str]] = field(default_factory=list)
    creates: int = 0

    def ensure_collection(self, section_key: str, name: str) -> str:
        self.ensure_calls.append((section_key, name))
        key = self.keys.get((section_key, name))
        if key is None:
            key = f"collection-{len(self.keys) + 1}"
            self.keys[(section_key, name)] = key
            self.creates += 1
            self.membership[key] = set()
        return key

    def add_to_collection(self, collection_key: str, rating_key: str) -> None:
        self.membership.setdefault(collection_key, set()).add(rating_key)

    def remove_from_collection(self, collection_key: str, rating_key: str) -> None:
        self.membership.get(collection_key, set()).discard(rating_key)

    def holds(self, rating_key: str) -> bool:
        return any(
            rating_key in members for members in self.membership.values()
        )


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


def seed_episode(
    conn,
    series_rating_key: str,
    season_number: int,
    episode_number: int,
    *,
    size_bytes: int = EPISODE_SIZE,
) -> str:
    rating_key = f"{series_rating_key}-s{season_number}e{episode_number}"
    conn.execute(
        "INSERT INTO episodes (plex_rating_key, series_rating_key, season_number,"
        " episode_number, file_size_bytes) VALUES (?, ?, ?, ?, ?)",
        (rating_key, series_rating_key, season_number, episode_number, size_bytes),
    )
    conn.commit()
    return rating_key


def seed_user(
    conn,
    plex_user_id: str,
    title: str,
    *,
    excluded: bool = False,
) -> int:
    cursor = conn.execute(
        "INSERT INTO users (plex_user_id, title, excluded) VALUES (?, ?, ?)",
        (plex_user_id, title, 1 if excluded else 0),
    )
    conn.commit()
    return int(cursor.lastrowid)


def seed_play(
    conn,
    user_id: int,
    rating_key: str,
    played_at: datetime,
    *,
    watched: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO play_events"
        " (user_id, plex_rating_key, watched, played_at, position_ms, duration_ms, source)"
        " VALUES (?, ?, ?, ?, ?, ?, 'plex')",
        (user_id, rating_key, 1 if watched else 0, played_at.isoformat(), 0, 0),
    )
    conn.commit()


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
        matches=(Match(category, f"{category.value} example reason"),),
        size_bytes=MOVIE_SIZE,
        rating=rating,
        facts={"matches": {category.value: {"example": "fact"}}},
    )


@dataclass
class StillMatches:
    """A rematcher stub answering one fixed verdict."""

    answer: bool = True
    calls: list[int] = field(default_factory=list)

    def still_matches(self, candidate: Candidate) -> bool:
        self.calls.append(candidate.media_item_id)
        return self.answer
