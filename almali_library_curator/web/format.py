"""Locale-aware display values for the screens (sizes, months, scan time)."""
from __future__ import annotations

from datetime import datetime, timedelta

from almali_library_curator.detection import text

# Decimal units, as the mockup shows them ("433 GB", "1.31 TB").
_UNITS = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
# Locales whose decimal separator is a comma.
_COMMA_LOCALES = frozenset({'ca', 'es'})


def human_size(size_bytes: int | None, locale: str) -> str:
    value = float(size_bytes or 0)
    unit = 0
    while value >= 1000 and unit < len(_UNITS) - 1:
        value /= 1000
        unit += 1
    if unit == 0 or value >= 10:
        number = f'{value:.0f}'
    else:
        number = f'{value:.2f}'.rstrip('0').rstrip('.')
    if locale in _COMMA_LOCALES:
        number = number.replace('.', ',')
    return f'{number} {_UNITS[unit]}'


def month_label(month: str | None, locale: str) -> str:
    """'2026-07' -> 'July 2026' in the locale's words; unparsable stays as is."""
    try:
        moment = datetime.strptime(month or '', '%Y-%m')
    except ValueError:
        return month or ''
    return text.month_year(locale, moment)


def scan_label(run: dict | None, locale: str, now: datetime) -> str | None:
    """'today 06:00' for a run that finished today, else '20 November 06:00'."""
    if not run or not run.get('finished_at'):
        return None
    try:
        finished = datetime.fromisoformat(run['finished_at'])
    except (TypeError, ValueError):
        return None
    return _moment_label(locale, finished, now)


def next_scan_label(scan_time: str, locale: str, now: datetime) -> str | None:
    """The next daily analysis, in the owner's words ('today 06:00').

    S4: the header names the analysis that is still to come, read from the
    configured daily time. An unreadable time answers nothing, so the header
    never shows a promise the scheduler cannot keep.
    """
    try:
        hour, minute = (int(part) for part in (scan_time or '').split(':'))
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (AttributeError, TypeError, ValueError):
        return None
    if slot < now:
        slot += timedelta(days=1)
    return _moment_label(locale, slot, now)


def _moment_label(locale: str, moment: datetime, now: datetime) -> str:
    """One instant as the owner reads the schedule: today, or its date."""
    if moment.tzinfo is not None and now.tzinfo is not None:
        moment = moment.astimezone(now.tzinfo)
    clock = moment.strftime('%H:%M')
    if moment.date() == now.date():
        return text.render(locale, 'scan.today', time=clock)
    return text.render(locale, 'scan.on_day', day=text.day_month(locale, moment), time=clock)


def plural(count: int, key: str) -> str:
    """The catalog key variant for a count: ``<key>.one`` or ``<key>.other``."""
    return f'{key}.one' if count == 1 else f'{key}.other'
