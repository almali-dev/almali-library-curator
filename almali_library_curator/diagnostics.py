"""The one-line diagnostic of a failure, safe to log and to store (#120).

Service error messages are external input and may carry credentials, so a
failure is named by its class only. A pydantic ``ValidationError`` adds what
the owner needs to see which payload was refused: the model and, for each
error, its location and type, never the refused value.

A location may name a dictionary key taken from the refused input, and such a
key can look exactly like a field name. Whether a segment is safe is therefore
decided by the schema, not by the shape of the string: the segment is kept only
if it is a list index or a declared field (or field alias) of the model reached
so far, and every segment from the first one that is not is masked as ``?``.
"""

from __future__ import annotations

from typing import Any, get_args

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

MAX_ERRORS = 3


def error_diagnostic(error: BaseException) -> str:
    """``ValueError``, or ``ValidationError: PlexMetadata ratingKey missing``.

    The location of each named error keeps only its list indices and the
    declared fields of the refused model; anything else, such as a dictionary
    key from the input, becomes ``?`` and so does everything after it.
    """
    name = type(error).__name__
    if not isinstance(error, ValidationError):
        return name
    details = error.errors(include_url=False, include_context=False, include_input=False)
    model = _refused_model(error.title)
    parts = [
        f"{'.'.join(_location_parts(detail['loc'], model)) or '-'} {detail['type']}"
        for detail in details[:MAX_ERRORS]
    ]
    if len(details) > MAX_ERRORS:
        parts.append(f"+{len(details) - MAX_ERRORS} more")
    return f"{name}: {error.title} {'; '.join(parts)}"


def _refused_model(title: str) -> type[BaseModel] | None:
    """The one ``BaseModel`` subclass named ``title``, or ``None``.

    An ambiguous name cannot say which schema applies, so every string
    segment of the location is masked.
    """
    found: list[type[BaseModel]] = []
    seen: set[type[BaseModel]] = set()

    def walk(model: type[BaseModel]) -> None:
        for child in model.__subclasses__():
            if child in seen:
                continue
            seen.add(child)
            if child.__name__ == title:
                found.append(child)
            walk(child)

    walk(BaseModel)
    return found[0] if len(found) == 1 else None


def _location_parts(loc: tuple[Any, ...], model: type[BaseModel] | None) -> list[str]:
    """The safe parts of ``loc``, from the schema reached so far."""
    parts: list[str] = []
    for part in loc:
        if model is None:
            parts.append("?")
        elif isinstance(part, int) and not isinstance(part, bool):
            parts.append(str(part))
        else:
            field = _declared_field(model, part) if isinstance(part, str) else None
            if field is None:
                parts.append("?")
                model = None
            else:
                parts.append(part)
                model = _nested_model(field.annotation)
    return parts


def _declared_field(model: type[BaseModel], part: str) -> FieldInfo | None:
    """The field of ``model`` that ``part`` names, by field name or alias."""
    for name, field in model.model_fields.items():
        if part == name:
            return field
        if part in (field.alias, field.serialization_alias, field.validation_alias):
            return field
    return None


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The single ``BaseModel`` reachable from a field annotation.

    ``Optional``/``Union``, list, tuple and dictionary values are looked
    through; no model, or more than one, means the next segment is masked.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    found: set[type[BaseModel]] = set()
    for argument in get_args(annotation):
        nested = _nested_model(argument)
        if nested is not None:
            found.add(nested)
    return found.pop() if len(found) == 1 else None
