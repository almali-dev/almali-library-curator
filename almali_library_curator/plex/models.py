"""Models for the Plex payloads.

Every Plex answer is external input, so it is validated before it reaches
the database.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    parts (a stacked, multi-disc film). The version carries its own
    ``videoResolution``, ``videoCodec`` and ``bitrate`` (kbps); every field
    is optional, because a server may omit any of them (#132).
    """

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    videoProfile: str | None = None
    videoResolution: str | None = None
    videoCodec: str | None = None
    bitrate: int | None = None
    Part: list[PlexPart] = Field(default_factory=list)

    @property
    def file_path(self) -> str | None:
        """The first file of the version."""
        return next((part.file for part in self.Part if part.file), None)

    @property
    def size_bytes(self) -> int | None:
        """The whole version: every part of a stacked film adds up.

        A version whose parts never report a size is unknown, never zero.
        """
        sizes = [part.size for part in self.Part if part.size is not None]
        return sum(sizes) if sizes else None


@dataclass(frozen=True)
class PlexVersion:
    """One version of an item and the facts the screens show (#132).

    It is one ``Media`` entry: the 4K file and the 1080p file of the same
    movie are two versions, and one of them may itself be a stacked film of
    several parts. A fact the server did not send stays ``None``: the app
    shows it as unknown instead of guessing a zero.
    """

    media_id: int | None
    video_profile: str | None
    resolution: str | None
    video_codec: str | None
    bitrate_kbps: int | None
    file_path: str | None
    size_bytes: int | None


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
    # #128: many TV and anime items under the new Plex agents carry only the
    # audience rating. It is the fallback when the critic ``rating`` is absent.
    audienceRating: float | None = None
    year: int | None = None
    # #139: the item's poster as a relative path such as
    # ``/library/metadata/11/thumb/1700000000``. It is stored as the server
    # sent it; whether it may become a poster request is decided when the
    # card renders, by the shared ``plex.posters.is_poster_thumb`` rule.
    thumb: str | None = None
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
    def versions(self) -> tuple[PlexVersion, ...]:
        """Every version of the item, in the order the server sent them.

        The item-level ``versions`` are exactly its ``Media`` entries. A
        payload with only a top-level ``Part`` describes no version, so it
        answers none and the item's own ``file_path``/``file_size`` stay the
        only facts it carries (#132).
        """
        return tuple(
            PlexVersion(
                media_id=media.id,
                video_profile=media.videoProfile,
                resolution=media.videoResolution,
                video_codec=media.videoCodec,
                bitrate_kbps=media.bitrate,
                file_path=media.file_path,
                size_bytes=media.size_bytes,
            )
            for media in self.Media
        )

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


class PlexHistoryEntry(PlexMetadata):
    """One watch history entry (#118).

    Plex keeps the plays of an item that was removed from the library but no
    longer links any item to them, so a history entry may come without a
    ``ratingKey``. Such an entry cannot match any library item; the sync
    skips it instead of refusing the whole page. Library items keep
    requiring the key.
    """

    ratingKey: str | None = None  # type: ignore[assignment]
