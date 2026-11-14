from __future__ import annotations

import pytest

from almali_library_curator.i18n import SUPPORTED_LOCALES, load_catalogs, translate


def test_translate_returns_the_catalog_value_per_locale():
    assert translate("nav.candidates", "en") == "Candidates"
    assert translate("nav.candidates", "ca") == "Candidats"
    assert translate("nav.candidates", "es") == "Candidatos"


def test_translate_falls_back_to_english_and_then_to_the_key():
    assert translate("nav.does_not_exist", "ca") == translate("nav.does_not_exist", "en")
    assert translate("nav.does_not_exist", "en") == "nav.does_not_exist"


def test_the_three_catalogs_hold_exactly_the_same_keys():
    """S2 rule 3: exact key parity across en, ca and es."""
    catalogs = load_catalogs()
    keys = {locale: set(catalogs[locale]) for locale in SUPPORTED_LOCALES}
    assert keys["en"] == keys["ca"] == keys["es"]


# S2: the scan-outcome panel and the wizard's step guidance.
FIELD_TEST_KEYS = (
    "scan.status.running",
    "scan.status.finished",
    "scan.status.failed",
    "scan.status.failed_hint",
    "scan.step.sync",
    "scan.step.enrichment",
    "scan.step.detection",
    "scan.step.recheck",
    "scan.step.deletion",
    "scan.step.finished",
    "scan.step.failed",
    "scan.step.skipped",
    "scan.step.pending",
    "setup.plex_done",
    "setup.libraries_done",
    "setup.libraries_scan_running",
    "setup.libraries_retry",
    "setup.libraries_run_scan",
)


# 0.33 T1, review round 4: the removal-failure wording must name every
# cause the code serves it for. ``files_unknown`` covers four distinct
# causes (unreadable media root, failed pre-stat, a scope no attempt saw,
# a scope file never visible) and must name the remedy; the Sonarr
# identity code covers both directions of the file-set mismatch.
_FILES_UNKNOWN_PHRASES = {
    "en": ["never seen by any attempt", "offline array disk", "wrong path map",
           "mark the item again"],
    "ca": ["no ha estat vist mai per cap intent", "un disc de la matriu desconnectat",
           "un mapatge de camins erroni", "torna a marcar l'element"],
    "es": ["nunca fue visto por ningún intento", "un disco de la matriz desconectado",
           "un mapa de rutas incorrecto", "vuelve a marcar el elemento"],
}
_SONARR_IDENTITY_PHRASES = {
    "en": ["outside the marked scope", "fewer files than the marked scope",
           "carries no path"],
    "ca": ["fora de l'abast marcat", "menys fitxers que l'abast marcat",
           "no inclou cap camí"],
    "es": ["fuera del alcance marcado", "menos archivos que el alcance marcado",
           "no incluye ninguna ruta"],
}


def test_the_removal_failure_wording_names_every_cause_and_the_remedy():
    catalogs = load_catalogs()
    for locale in SUPPORTED_LOCALES:
        unknown = catalogs[locale]["removal.failed.files_unknown"]
        for phrase in _FILES_UNKNOWN_PHRASES[locale]:
            assert phrase in unknown, (locale, phrase)
        identity = catalogs[locale]["removal.failed.sonarr_identity"]
        for phrase in _SONARR_IDENTITY_PHRASES[locale]:
            assert phrase in identity, (locale, phrase)


@pytest.mark.parametrize("key", FIELD_TEST_KEYS)
@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_the_scan_and_wizard_keys_exist_in_every_catalog(key, locale):
    """S2 rule 3: every new string exists in en, ca and es, never empty."""
    value = translate(key, locale)
    assert value != key
    assert value.strip()
