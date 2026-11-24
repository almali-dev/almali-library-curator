"""Models for the Tautulli payloads.

Every Tautulli answer is external input, so it is validated before it
reaches the rest of the app.
"""

from __future__ import annotations

from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator


def _is_optional_number(annotation: Any) -> bool:
    """True for ``int | None``: a number Tautulli may report as no value."""
    arguments = get_args(annotation)
    return int in arguments and type(None) in arguments


class TautulliUser(BaseModel):
    """One account known to Tautulli (get_users)."""

    model_config = ConfigDict(extra="ignore")

    user_id: int
    username: str = ""
    friendly_name: str | None = None
    email: str | None = None
    thumb: str | None = None
    is_admin: int | None = None
    is_home_user: int | None = None
    is_allow_sync: int | None = None
    is_restricted: int | None = None
    keep_history: int | None = None
    allow_guest: int | None = None
    deleted_user: int | None = None


class TautulliHistoryEntry(BaseModel):
    """One row of per-user play history (get_history).

    `rating_key` is the Plex rating key of the played item, so this row is
    what Tautulli contributes to the longer and more reliable per-user
    history (S2).
    """

    model_config = ConfigDict(extra="ignore")

    # #119: Tautulli may list a row with a null id (and, rarely, a null
    # rating_key); one such row must not refuse the whole history page.
    id: int | None = None
    date: int | None = None
    started: int | None = None
    stopped: int | None = None
    paused_counter: int | None = None
    rating_key: int | None = None
    user_id: int | None = None
    user: str | None = None
    friendly_name: str | None = None
    full_title: str = ""
    title: str | None = None
    grandparent_title: str | None = None
    parent_media_index: int | None = None
    media_index: int | None = None
    media_type: str | None = None
    view_offset: int | None = None
    duration: int | None = None
    percent_complete: int | None = None
    transcode_decision: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_number_is_no_value(cls, value, info: ValidationInfo):
        """#125: Tautulli sends ``''`` for a number a movie row does not have.

        A movie has no episode or season number, so its ``media_index`` and
        ``parent_media_index`` arrive as an empty string. The model already
        says those fields are optional, so an empty string is read as the no
        value it means instead of refusing the whole history page.
        """
        if value == "" and _is_optional_number(cls.model_fields[info.field_name].annotation):
            return None
        return value
