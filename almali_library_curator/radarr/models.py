"""Models for the Radarr v3 payloads.

Every Radarr answer is external input, so it is validated before it
reaches the rest of the app. Field names follow the Radarr payloads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RadarrSystemStatus(BaseModel):
    """The answer of GET /api/v3/system/status."""

    model_config = ConfigDict(extra="ignore")

    appName: str | None = None
    version: str | None = None
    authentication: str | None = None


class RadarrMovie(BaseModel):
    """A movie resource."""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    title: str = ""
    tmdbId: int | None = None
    imdbId: str | None = None
    monitored: bool = False
    year: int | None = None
    path: str | None = None
    titleSlug: str | None = None
    isAvailable: bool | None = None
    hasFile: bool | None = None
    sizeOnDisk: int | None = None
    added: str | None = None


class RadarrImportExclusion(BaseModel):
    """An entry of the import exclusion list.

    Radarr names the movie fields ``movieTitle`` and ``movieYear``; the
    payload does not carry the Sonarr ``title`` field.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    tmdbId: int | None = None
    movieTitle: str = ""
    movieYear: int | None = None
