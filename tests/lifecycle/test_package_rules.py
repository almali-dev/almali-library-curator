"""Structural rules of the lifecycle package and the catalogs it uses."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ARR_PACKAGES = (
    "almali_library_curator.sonarr",
    "almali_library_curator.radarr",
)


def test_the_lifecycle_package_imports_neither_arr_client():
    """The lifecycle never talks to Sonarr or Radarr (S5 step 5 is T7)."""
    code = (
        "import sys\n"
        "import almali_library_curator.lifecycle\n"
        "loaded = [name for name in sys.modules"
        " if name.startswith('almali_library_curator.sonarr')"
        " or name.startswith('almali_library_curator.radarr')]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_the_lifecycle_source_never_names_the_arr_packages():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "almali_library_curator" / "lifecycle").glob("*.py")
    )
    assert "sonarr" not in source.lower()
    assert "radarr" not in source.lower()


def test_the_three_catalogs_carry_identical_key_sets():
    from almali_library_curator.i18n import SUPPORTED_LOCALES, load_catalogs

    catalogs = load_catalogs()
    keys = {locale: set(catalogs[locale]) for locale in SUPPORTED_LOCALES}
    assert keys["en"] == keys["ca"] == keys["es"]


def test_the_started_watching_copy_is_exactly_as_specified():
    from almali_library_curator.i18n import translate

    assert (
        translate("removal.cancel.started_watching", "en")
        == "Removal cancelled: {name} started watching on {date}"
    )
