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


@pytest.mark.parametrize("key", FIELD_TEST_KEYS)
@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_the_scan_and_wizard_keys_exist_in_every_catalog(key, locale):
    """S2 rule 3: every new string exists in en, ca and es, never empty."""
    value = translate(key, locale)
    assert value != key
    assert value.strip()
