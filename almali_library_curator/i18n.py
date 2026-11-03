"""Translation catalogs.

English, Catalan and Spanish from day one. Every user-facing string goes
through a translation key; no hard-coded strings in templates or scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ca", "es")
CATALOG_DIR = Path(__file__).parent / "i18n"

_catalogs: dict[str, dict[str, str]] | None = None


def load_catalogs(directory: Path | None = None) -> dict[str, dict[str, str]]:
    """Load the translation catalogs, caching the default directory."""
    global _catalogs
    if directory is None and _catalogs is not None:
        return _catalogs

    base = directory if directory is not None else CATALOG_DIR
    catalogs: dict[str, dict[str, str]] = {}
    for locale in SUPPORTED_LOCALES:
        path = base / f"{locale}.json"
        if not path.exists():
            catalogs[locale] = {}
            continue
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            content = {}
        catalogs[locale] = content if isinstance(content, dict) else {}

    if directory is None:
        _catalogs = catalogs
    return catalogs


def translate(
    key: str,
    locale: str = DEFAULT_LOCALE,
    catalogs: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Return the translation for ``key``.

    Falls back to English and then to the key itself, so a missing translation
    is visible but never fatal.
    """
    table = load_catalogs() if catalogs is None else catalogs
    value = (table.get(locale) or {}).get(key)
    if value is not None:
        return value
    fallback = (table.get(DEFAULT_LOCALE) or {}).get(key)
    if fallback is not None:
        return fallback
    return key
