"""Models for the TMDB payloads.

Every TMDB answer is external input, so it is validated before it reaches
the rest of the app. TMDB already uses snake_case keys, so the models keep
the field names of the API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TmdbMovie(BaseModel):
    """A movie's details (/3/movie/{id})."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""
    original_title: str | None = None
    vote_average: float = 0.0
    vote_count: int | None = None
    status: str = ""
    release_date: str | None = None


class TmdbShow(BaseModel):
    """A series' details (/3/tv/{id}).

    ``status`` carries TMDB's production status ("Returning Series",
    "Ended", "Canceled", ...), which the detection layer maps to
    continuing/ended/cancelled (S3).
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = ""
    original_name: str | None = None
    vote_average: float = 0.0
    vote_count: int | None = None
    status: str = ""
    first_air_date: str | None = None
    last_air_date: str | None = None
    number_of_seasons: int | None = None
