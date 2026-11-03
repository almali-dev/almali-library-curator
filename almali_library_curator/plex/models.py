"""Models for the Plex payloads.

Every Plex answer is external input, so it is validated before it reaches
the database.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# An item counts as watched when it was played past 90% of its duration.
WATCHED_RATIO = 0.9


class PlexSection(BaseModel):
    """A library section."""

    model_config = ConfigDict(extra="ignore")

    key: str
    title: str = ""
    type: str | None = None
    agent: str | None = None
    uuid: str | None = None


class PlexAccount(BaseModel):
    """A Plex account with access to the server."""

    model_config = ConfigDict(extra="ignore")

    id: int | str
    name: str | None = None
    title: str | None = None
    email: str | None = None


class PlexPart(BaseModel):
    """One file of an item."""

    model_config = ConfigDict(extra="ignore")

    file: str | None = None
    size: int | None = None
    key: str | None = None


class PlexMetadata(BaseModel):
    """An item, a child item or a history entry."""

    model_config = ConfigDict(extra="ignore")

    ratingKey: str
    key: str | None = None
    title: str = ""
    type: str | None = None
    parentRatingKey: str | None = None
    grandparentRatingKey: str | None = None
    index: int | None = None
    addedAt: int | None = None
    lastViewedAt: int | None = None
    viewedAt: int | None = None
    rating: float | None = None
    contentRating: str | None = None
    leafCount: int | None = None
    viewedLeafCount: int | None = None
    childCount: int | None = None
    viewedChildCount: int | None = None
    duration: int | None = None
    viewOffset: int | None = None
    accountID: int | None = None
    Part: list[PlexPart] = Field(default_factory=list)

    @property
    def file_path(self) -> str | None:
        for part in self.Part:
            if part.file:
                return part.file
        return None

    @property
    def file_size(self) -> int | None:
        for part in self.Part:
            if part.size is not None:
                return part.size
        return None


def is_watched(position_ms: int | None, duration_ms: int | None) -> bool:
    """True when an item counts as watched.

    S3: "an episode or movie marked watched in Plex, or played past 90% in
    history".
    """
    if not duration_ms or duration_ms <= 0:
        return False
    return (position_ms or 0) >= WATCHED_RATIO * duration_ms
