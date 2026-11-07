"""The protected reasons render through en, ca and es templates."""

from __future__ import annotations

import pytest

from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from helpers import default_settings, make_candidate, seed_item, seed_library

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


@pytest.mark.parametrize("locale", ["en", "ca", "es"])
def test_each_protection_reason_is_localized(conn, locale):
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

    reasons = {entry.protection.kind: entry.reason for entry in outcome.protected}
    expected = EXPECTED_REASONS[locale]
    assert reasons[ProtectionKind.LIBRARY] == expected["library"]
    assert reasons[ProtectionKind.RATING] == expected["rating"]
    assert reasons[ProtectionKind.KEEP_FOREVER] == expected["keep_forever"]
