"""The one-line diagnostic of a failure, safe to log and to store (#120).

Service error messages are external input and may carry credentials, so a
failure is named by its class only. A pydantic ``ValidationError`` adds what
the owner needs to see which payload was refused: the model and, for each
error, its location and type, never the refused value.
"""

from __future__ import annotations

from pydantic import ValidationError

MAX_ERRORS = 3
MAX_LOCATION_PART = 40


def error_diagnostic(error: BaseException) -> str:
    """``ValueError``, or ``ValidationError: PlexMetadata ratingKey missing``."""
    name = type(error).__name__
    if not isinstance(error, ValidationError):
        return name
    details = error.errors(include_url=False, include_context=False, include_input=False)
    parts = [
        f"{'.'.join(_location_part(part) for part in detail['loc']) or '-'} {detail['type']}"
        for detail in details[:MAX_ERRORS]
    ]
    if len(details) > MAX_ERRORS:
        parts.append(f"+{len(details) - MAX_ERRORS} more")
    return f"{name}: {error.title} {'; '.join(parts)}"


def _location_part(part: str | int) -> str:
    """A field name or a list index; anything else came from the input.

    A dictionary key is part of the location too, and it is refused input,
    so a part that does not look like a field name is masked.
    """
    if isinstance(part, int):
        return str(part)
    if part.isidentifier() and len(part) <= MAX_LOCATION_PART:
        return part
    return "?"
