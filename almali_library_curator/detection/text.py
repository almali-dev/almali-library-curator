"""Locale-aware fragments for the reason sentences.

Every user-facing word goes through a translation key (S9): month names,
dates, list joins and the full sentence templates. User display names,
counts and season labels interpolate into the translated templates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import TypedDict

from almali_library_curator.i18n import translate

# The mockup titles seasons with this prefix and an en dash between runs.
SEASON_PREFIX = "S"
SEASON_RANGE = "–"


class ReasonPart(TypedDict):
    """One sentence fragment: a translation key plus its raw parameters.

    The parameters are what was observed (an ISO date, a user name, the
    season numbers, a count), never the locale's words for it, so the
    fragment can be rendered in whatever language is asked for later.
    """

    key: str
    params: dict[str, object]


# The kinds of raw parameter the renderer formats. Every key that carries
# more than a plain value declares, parameter by parameter, how its raw
# value becomes locale text; a parameter absent from the table is a plain
# value and interpolates as it is.
KIND_MONTH_YEAR = "month_year"
KIND_SEASON_LABEL = "season_label"
KIND_SEASON_NAMES = "season_names"
KIND_SEASON_RUNS = "season_runs"
KIND_NAME_LIST = "name_list"
KIND_RATING = "rating"

PARAM_KINDS: dict[str, dict[str, str]] = {
    "detection.reason.stalled.singular": {
        "season": KIND_SEASON_LABEL,
        "date": KIND_MONTH_YEAR,
        "unopened": KIND_SEASON_NAMES,
    },
    "detection.reason.stalled.plural": {
        "season": KIND_SEASON_LABEL,
        "date": KIND_MONTH_YEAR,
        "unopened": KIND_SEASON_NAMES,
    },
    "detection.reason.never_watched.series": {"date": KIND_MONTH_YEAR},
    "detection.reason.never_watched.movie": {"date": KIND_MONTH_YEAR},
    "detection.reason.never_watched.series_no_plays": {"date": KIND_MONTH_YEAR},
    "detection.reason.never_watched.movie_no_plays": {"date": KIND_MONTH_YEAR},
    "detection.reason.cancelled.played": {"date": KIND_MONTH_YEAR},
    "detection.reason.cancelled.played_no_year": {"date": KIND_MONTH_YEAR},
    "detection.reason.everyone_finished.one": {"seasons": KIND_SEASON_RUNS},
    "detection.reason.everyone_finished.two": {"seasons": KIND_SEASON_RUNS},
    "detection.reason.everyone_finished.many": {
        "names": KIND_NAME_LIST,
        "seasons": KIND_SEASON_RUNS,
    },
    "detection.reason.pending.singular": {"seasons": KIND_SEASON_NAMES},
    "detection.reason.pending.plural": {"seasons": KIND_SEASON_NAMES},
    "detection.reason.running.stays": {"season": KIND_SEASON_LABEL},
    "detection.reason.not_rewatched.once": {"date": KIND_MONTH_YEAR},
    "detection.reason.not_rewatched.twice": {"date": KIND_MONTH_YEAR},
    "detection.reason.not_rewatched.times": {
        "names": KIND_NAME_LIST,
        "date": KIND_MONTH_YEAR,
    },
    "protection.reason.rating": {"minimum": KIND_RATING},
}


def render(locale: str, key: str, **params: object) -> str:
    """Translate ``key`` and interpolate its parameters."""
    return translate(key, locale).format(**params)


def reason_part(key: str, **params: object) -> ReasonPart:
    """A reason fragment carrying ``key`` and its raw parameters."""
    return {"key": key, "params": dict(params)}


def render_reason(locale: str, parts: Iterable[Mapping[str, object]]) -> str:
    """Render stored reason parts in ``locale``.

    The parts are joined with a space, the way a multi-clause reason reads
    as one sentence. Every raw parameter declared in ``PARAM_KINDS`` is
    formatted here, at render time, so the same stored parts answer with
    the requested locale's own dates, lists and numbers.
    """
    return " ".join(_render_part(locale, part) for part in parts)


def _render_part(locale: str, part: Mapping[str, object]) -> str:
    key = str(part["key"])
    raw = part.get("params") or {}
    kinds = PARAM_KINDS.get(key, {})
    params = {
        name: _render_param(locale, kinds.get(name), value)
        for name, value in raw.items()
    }
    return render(locale, key, **params)


def _render_param(locale: str, kind: str | None, value: object) -> object:
    """One raw parameter as the locale's text, by its declared kind."""
    if kind == KIND_MONTH_YEAR:
        return month_year(locale, _as_datetime(value))
    if kind == KIND_SEASON_LABEL:
        return f"{SEASON_PREFIX}{value}"
    if kind == KIND_SEASON_NAMES:
        return season_names(locale, _as_seasons(value))
    if kind == KIND_SEASON_RUNS:
        return season_runs(_as_seasons(value))
    if kind == KIND_NAME_LIST:
        return join_and(locale, [str(name) for name in _as_list(value)])
    if kind == KIND_RATING:
        return format_rating(value)
    return value


def _as_datetime(value: object) -> datetime:
    """A raw ISO date string, or an already parsed moment."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _as_list(value: object) -> Sequence[object]:
    if isinstance(value, (list, tuple)):
        return value
    return [value]


def _as_seasons(value: object) -> list[int]:
    return [int(season) for season in _as_list(value)]


def format_rating(value: object) -> str:
    """A rating like 8.7 or 8.0, for interpolation into reasons."""
    return str(float(value))


def month_year(locale: str, moment: datetime) -> str:
    """A month and year like "March 2025", in the locale's own words."""
    month = translate(f"detection.month.{moment.month}", locale)
    return render(locale, "detection.date.month_year", month=month, year=moment.year)


def day_month(locale: str, moment: datetime) -> str:
    """A day and month like "30 October", in the locale's own words.

    Catalan contracts "de" to "d'" before a vowel-initial month, so the
    vowel variant of the template carries that contraction and is only
    used where the grammar needs it.
    """
    month = translate(f"detection.month.{moment.month}", locale)
    key = "detection.date.day_month"
    if locale == "ca" and month[:1].lower() in "aeiouàèéíòóú":
        key = "detection.date.day_month_vowel"
    return render(locale, key, day=moment.day, month=month)


def day_month_year(locale: str, moment: datetime) -> str:
    """A whole date like "30 October 2026", in the locale's own words."""
    return f"{day_month(locale, moment)} {moment.year}"


def join_and(locale: str, parts: list[str]) -> str:
    """Join names or season labels with the locale's "and"."""
    if len(parts) <= 1:
        return "".join(parts)
    and_word = translate("detection.join.and", locale)
    if len(parts) == 2:
        return f"{parts[0]} {and_word} {parts[1]}"
    return ", ".join(parts[:-1]) + f" {and_word} {parts[-1]}"


def season_runs(seasons: list[int]) -> str:
    """Season numbers as the mockup titles them: 1 -> "S1", [2, 3] -> "S2–S3"."""
    if not seasons:
        return ""
    ordered = sorted(seasons)
    runs: list[list[int]] = [[ordered[0]]]
    for number in ordered[1:]:
        if number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    labels = [
        f"{SEASON_PREFIX}{run[0]}"
        if len(run) == 1
        else f"{SEASON_PREFIX}{run[0]}{SEASON_RANGE}{SEASON_PREFIX}{run[-1]}"
        for run in runs
    ]
    return ", ".join(labels)


def season_names(locale: str, seasons: list[int]) -> str:
    """Season numbers as a spoken list: [2, 3] -> "S2 and S3"."""
    return join_and(locale, [f"{SEASON_PREFIX}{number}" for number in sorted(seasons)])
