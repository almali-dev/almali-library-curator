"""Locale-aware fragments for the reason sentences.

Every user-facing word goes through a translation key (S9): month names,
dates, list joins and the full sentence templates. User display names,
counts and season labels interpolate into the translated templates.
"""

from __future__ import annotations

from datetime import datetime

from almali_library_curator.i18n import translate

# The mockup titles seasons with this prefix and an en dash between runs.
SEASON_PREFIX = "S"
SEASON_RANGE = "–"


def render(locale: str, key: str, **params: object) -> str:
    """Translate ``key`` and interpolate its parameters."""
    return translate(key, locale).format(**params)


def month_year(locale: str, moment: datetime) -> str:
    """A month and year like "March 2025", in the locale's own words."""
    month = translate(f"detection.month.{moment.month}", locale)
    return render(locale, "detection.date.month_year", month=month, year=moment.year)


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
