"""Models for the Sonarr v3 payloads.

Every Sonarr answer is external input, so it is validated before it
reaches the rest of the app. Field names follow the Sonarr payloads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SonarrSystemStatus(BaseModel):
    """The answer of GET /api/v3/system/status."""

    model_config = ConfigDict(extra="ignore")

    appName: str | None = None
    version: str | None = None
    authentication: str | None = None


class SonarrSeasonStatistics(BaseModel):
    """The statistics block of one season."""

    model_config = ConfigDict(extra="ignore")

    episodeFileCount: int | None = None
    episodeCount: int | None = None
    totalEpisodeCount: int | None = None
    sizeOnDisk: int | None = None
    percentOfEpisodes: float | None = None


class SonarrSeason(BaseModel):
    """One season of a series."""

    model_config = ConfigDict(extra="ignore")

    seasonNumber: int
    monitored: bool | None = None
    statistics: SonarrSeasonStatistics | None = None


class SonarrSeries(BaseModel):
    """A series resource; the status fields drive the detection categories."""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    title: str = ""
    status: str | None = None
    ended: bool | None = None
    monitored: bool = False
    tvdbId: int | None = None
    seasonFolder: bool | None = None
    path: str | None = None
    year: int | None = None
    titleSlug: str | None = None
    seasons: list[SonarrSeason] = Field(default_factory=list)


class SonarrEpisodeFile(BaseModel):
    """The identity and season of a file returned by the episodefile endpoint."""

    model_config = ConfigDict(extra="ignore")

    id: int
    seriesId: int
    seasonNumber: int
    path: str | None = None


class SonarrImportExclusion(BaseModel):
    """An entry of the import exclusion list."""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    tvdbId: int | None = None
    title: str = ""
