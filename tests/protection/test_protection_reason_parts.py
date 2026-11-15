"""Protection reasons also carry a key plus raw parameters (#47, S1a).

The three protections keep the library title, the item's detail and the
minimum rating as raw values; the localized sentence is rendered from them,
and must stay the exact text the Protected view already shows.
"""

from __future__ import annotations

import json

import pytest

from almali_library_curator.detection import text
from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library

LOCALES = ("en", "ca", "es")

EXPECTED_REASONS = {
    "en": {
        "library": "The protected library Filmoteca covers it.",
        "rating": "Rated 8.7, at or above the minimum of 8.0.",
        "keep_forever": "Kept forever by hand.",
    },
    "ca": {
        "library": "Coberta per la biblioteca protegida Filmoteca.",
        "rating": "Amb una valoració de 8.7, igual o superior al mínim de 8.0.",
        "keep_forever": "Mantinguda per sempre manualment.",
    },
    "es": {
        "library": "Cubierta por la biblioteca protegida Filmoteca.",
        "rating": "Con una valoración de 8.7, igual o superior al mínimo de 8.0.",
        "keep_forever": "Mantenida para siempre manualmente.",
    },
}


def _protected_reasons(conn, locale: str) -> dict[ProtectionKind, object]:
    protected_library = seed_library(conn, "3", "Filmoteca", protected=True)
    open_library = seed_library(conn, "1", "Movies")
    library_item = seed_item(conn, protected_library, "300", "Blade Runner")
    rating_item = seed_item(conn, open_library, "100", "Severance")
    kept_item = seed_item(conn, open_library, "101", "The Office (US)")

    library_candidate = make_candidate(library_item, "300", "Blade Runner")
    rating_candidate = make_candidate(rating_item, "100", "Severance", rating=8.7)
    kept_candidate = make_candidate(kept_item, "101", "The Office (US)")

    engine = ProtectionEngine(conn, settings=default_settings(locale=locale))
    engine.keep_forever(kept_candidate)
    outcome = engine.protect([library_candidate, rating_candidate, kept_candidate])
    return {entry.protection.kind: entry for entry in outcome.protected}


@pytest.mark.parametrize("locale", LOCALES)
def test_each_protection_renders_its_parts_into_the_stored_sentence(conn, locale):
    protected = _protected_reasons(conn, locale)
    expected = EXPECTED_REASONS[locale]

    for kind in (ProtectionKind.LIBRARY, ProtectionKind.RATING, ProtectionKind.KEEP_FOREVER):
        entry = protected[kind]
        assert text.render_reason(locale, entry.reason_parts) == entry.reason
        assert entry.reason == expected[kind.value]


@pytest.mark.parametrize("locale", LOCALES)
def test_protection_parts_keep_the_detail_and_the_minimum_rating_raw(conn, locale):
    protected = _protected_reasons(conn, locale)

    (library_part,) = protected[ProtectionKind.LIBRARY].reason_parts
    assert library_part == {
        "key": "protection.reason.library",
        "params": {"library": "Filmoteca"},
    }

    (rating_part,) = protected[ProtectionKind.RATING].reason_parts
    assert rating_part["key"] == "protection.reason.rating"
    assert rating_part["params"]["rating"] == "8.7"
    # The minimum stays a number; the locale text is applied at render time.
    assert rating_part["params"]["minimum"] == 8.0
    assert isinstance(rating_part["params"]["minimum"], float)

    (kept_part,) = protected[ProtectionKind.KEEP_FOREVER].reason_parts
    assert kept_part == {"key": "protection.reason.keep_forever", "params": {}}

    for entry in protected.values():
        stored = json.loads(json.dumps(entry.reason_parts))
        assert text.render_reason(locale, stored) == entry.reason


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        (
            "protection.reason.library",
            {"library": "Filmoteca"},
            lambda locale: {"library": "Filmoteca"},
        ),
        (
            "protection.reason.rating",
            {"rating": "8.7", "minimum": 8.0},
            lambda locale: {
                "rating": "8.7",
                "minimum": text.format_rating(8.0),
            },
        ),
        (
            "protection.reason.keep_forever",
            {},
            lambda locale: {},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_renderer_formats_the_minimum_rating(locale, key, raw, expected):
    parts = [{"key": key, "params": raw}]

    assert text.render_reason(locale, parts) == text.render(
        locale, key, **expected(locale)
    )
