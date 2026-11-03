from __future__ import annotations

from almali_library_curator.i18n import translate


def test_translate_returns_the_catalog_value_per_locale():
    assert translate("nav.candidates", "en") == "Candidates"
    assert translate("nav.candidates", "ca") == "Candidats"
    assert translate("nav.candidates", "es") == "Candidatos"


def test_translate_falls_back_to_english_and_then_to_the_key():
    assert translate("nav.does_not_exist", "ca") == translate("nav.does_not_exist", "en")
    assert translate("nav.does_not_exist", "en") == "nav.does_not_exist"
