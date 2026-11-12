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
    """One file of an item.

    A live server answers each part with ``container, duration, file, id,
    indexes, key, size, videoProfile``; only the fields this app consumes
    are validated, the rest is ignored.
    """

    model_config = ConfigDict(extra="ignore")

    file: str | None = None
    size: int | None = None
    key: str | None = None


class PlexMedia(BaseModel):
    """One media stream of an item, which carries its files as parts.

    A media entry is one version of the item (for example the 4K and the
    1080p file of the same movie), and each version can carry several
    parts (a stacked, multi-disc film).
    """

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    videoProfile: str | None = None
    Part: list[PlexPart] = Field(default_factory=list)


class PlexGuid(BaseModel):
    """An external agent identity requested with includeGuids=1."""

    model_config = ConfigDict(extra="ignore")

    id: str


class PlexMetadata(BaseModel):
    """An item, a child item or a history entry.

    File paths and sizes live in ``Media[].Part[]`` on a live server. A
    top-level ``Part`` list is still read, but only as a documented
    fallback for older payload shapes that carry it, so those servers
    keep working.
    """

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
    year: int | None = None
    Guid: list[PlexGuid] = Field(default_factory=list)
    contentRating: str | None = None
    leafCount: int | None = None
    viewedLeafCount: int | None = None
    childCount: int | None = None
    viewedChildCount: int | None = None
    duration: int | None = None
    viewOffset: int | None = None
    accountID: int | None = None
    Media: list[PlexMedia] = Field(default_factory=list)
    Part: list[PlexPart] = Field(default_factory=list)

    @property
    def external_ids(self) -> dict[str, int | str | None]:
        """Only recognized, well-formed agent ids are eligible for linking."""
        ids: dict[str, int | str | None] = {
            "tmdb_id": None, "tvdb_id": None, "imdb_id": None,
        }
        for guid in self.Guid:
            provider, separator, value = guid.id.partition("://")
            if not separator:
                continue
            if provider in ("tmdb", "tvdb") and value.isascii() and value.isdecimal():
                if int(value) > 0:
                    ids[f"{provider}_id"] = int(value)
            elif provider == "imdb" and value.startswith("tt") and value[2:].isdigit():
                ids["imdb_id"] = value
        return ids

    @property
    def all_parts(self) -> list[PlexPart]:
        """Every file of the item, in the order the server sent them.

        Selection rule for the primary file when an item has several
        versions (``Media`` entries) or several parts per version: the
        first part of the first ``Media`` entry in document order wins,
        because Plex lists the primary version first. A top-level ``Part``
        is only consulted when the item carries no ``Media`` at all.
        """
        if self.Media:
            return [part for media in self.Media for part in media.Part]
        return list(self.Part)

    @property
    def file_path(self) -> str | None:
        for part in self.all_parts:
            if part.file:
                return part.file
        return None

    @property
    def file_size(self) -> int | None:
        for part in self.all_parts:
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
