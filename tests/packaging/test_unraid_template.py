"""Tests of the Unraid Community Applications template (T13, S8).

The template must parse, carry every element CA expects, and point at an icon
that really exists in the repository (PSC issue #850: its template referenced
an asset that was never committed).
"""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "unraid-template.xml"

REPOSITORY = "ghcr.io/almali-dev/almali-library-curator"
PROJECT_URL = "https://github.com/almali-dev/almali-library-curator"
RAW_BASE = "https://raw.githubusercontent.com/almali-dev/almali-library-curator/main"

# The Plex disclaimer of S10, verbatim.
DISCLAIMER = "Not affiliated with or endorsed by Plex Inc."


@pytest.fixture(scope="module")
def tree():
    assert TEMPLATE.is_file(), "unraid-template.xml is missing"
    return ET.parse(TEMPLATE)


@pytest.fixture(scope="module")
def root(tree):
    return tree.getroot()


def test_the_template_is_valid_xml(root):
    assert root.tag == "Container"
    assert root.get("version") == "2"


def text_of(root, tag: str) -> str:
    element = root.find(tag)
    assert element is not None, f"<{tag}> is missing"
    return (element.text or "").strip()


def test_the_template_carries_every_required_element(root):
    assert text_of(root, "Name") == "almali-library-curator"
    assert text_of(root, "Repository") == f"{REPOSITORY}:latest"
    assert text_of(root, "Registry").startswith(PROJECT_URL)
    assert text_of(root, "Support").startswith(PROJECT_URL)
    assert text_of(root, "Project") == PROJECT_URL
    assert text_of(root, "TemplateURL") == f"{RAW_BASE}/unraid-template.xml"
    assert text_of(root, "Category")
    assert text_of(root, "WebUI") == "http://[IP]:[PORT:17540]/"


def test_the_overview_states_the_plex_disclaimer(root):
    overview = text_of(root, "Overview")
    assert DISCLAIMER in overview


def test_the_icon_points_at_a_file_that_exists_in_the_repository(root):
    icon = text_of(root, "Icon")
    assert icon.startswith(RAW_BASE + "/"), icon
    relative = icon[len(RAW_BASE) + 1 :]
    assert (ROOT / relative).is_file(), f"{relative} is not committed in the repository"


def configs(root) -> dict[str, ET.Element]:
    return {element.get("Target"): element for element in root.findall("Config")}


def test_the_template_has_every_config_entry(root):
    entries = configs(root)
    for target in (
        "17540",
        "/config",
        "/media",
        "/unraid/mnt",
        "PUID",
        "PGID",
        "TZ",
        "ALC_AUTH_USER",
        "ALC_AUTH_PASSWORD",
        "ALC_UNRAID_ROOT",
        "ALC_PLEX_PATH_MAP",
        "ALC_TRUSTED_PROXIES",
    ):
        assert target in entries, f"the Config entry {target} is missing"


def test_path_map_description_example_is_accepted_by_the_app(root, tmp_path):
    from almali_library_curator.config import load_settings

    description = configs(root)["ALC_PLEX_PATH_MAP"].get("Description")
    assert "<plex_prefix>=<container_prefix>" in description
    assert "separated by ','" in description
    assert "/data=/media" in description
    example = description.split("for example ", 1)[1].split(". ", 1)[0]
    assert load_settings(tmp_path, env={"ALC_PLEX_PATH_MAP": example}).plex_path_map == (
        ("/data", "/media"), ("/tv", "/media/tv"))


def test_the_port_entry_is_a_tcp_port(root):
    port = configs(root)["17540"]
    assert port.get("Type") == "Port"
    assert port.get("Mode") == "tcp"
    assert port.get("Default") == "17540"


def test_the_media_and_unraid_mounts_are_read_only(root):
    entries = configs(root)
    assert entries["/media"].get("Mode") == "ro"
    assert entries["/media"].get("Type") == "Path"
    assert entries["/unraid/mnt"].get("Mode") == "ro"
    assert entries["/unraid/mnt"].get("Type") == "Path"
    assert entries["/config"].get("Mode") == "rw"


def test_the_password_entry_is_masked_and_required(root):
    password = configs(root)["ALC_AUTH_PASSWORD"]
    assert password.get("Mask") == "true"
    assert password.get("Required") == "true"
    assert not (password.text or "").strip(), "the password entry carries a default"


def test_the_owner_user_entry_is_required(root):
    user = configs(root)["ALC_AUTH_USER"]
    assert user.get("Required") == "true"


def test_the_advanced_entries_are_hidden_by_default(root):
    entries = configs(root)
    assert entries["ALC_TRUSTED_PROXIES"].get("Display") == "advanced"
    assert entries["ALC_PLEX_PATH_MAP"].get("Display") == "advanced"
    assert entries["ALC_UNRAID_ROOT"].get("Display") == "advanced"


# --- #71: the run options the template hands to Docker ------------------


def extra_params(root) -> str:
    return text_of(root, "ExtraParams") if root.find("ExtraParams") is not None else ""


WATCHTOWER_LABEL = "--label com.centurylinklabs.watchtower.enable=true"


def test_the_template_opts_into_watchtower_updates(root):
    """The owner's deployment model: a template install follows `latest` and a
    label-scoped Watchtower updates it, as Plex Smart Cache does."""
    assert text_of(root, "Repository") == f"{REPOSITORY}:latest"
    assert extra_params(root).count(WATCHTOWER_LABEL) == 1


def test_the_template_hardens_the_container_run_options(root):
    params = extra_params(root)
    # Least privilege at run time, with the capabilities the entrypoint needs
    # to fix /config ownership and drop to PUID:PGID.
    assert "--cap-drop ALL" in params
    assert "--cap-add CHOWN" in params
    assert "--cap-add DAC_OVERRIDE" in params
    assert "--cap-add SETUID" in params
    assert "--cap-add SETGID" in params
    assert "--security-opt no-new-privileges:true" in params
    assert "--read-only" in params
    assert "--tmpfs /tmp" in params


def test_the_password_entry_points_at_the_hashed_or_file_sources(root):
    """#71: the masked variable stays, and the description names the safer ones."""
    description = configs(root)["ALC_AUTH_PASSWORD"].get("Description")
    assert "ALC_AUTH_PASSWORD_FILE" in description
    assert "ALC_AUTH_PASSWORD_HASH" in description


def test_the_unraid_root_variable_names_the_mounted_path(root):
    entries = configs(root)
    assert entries["ALC_UNRAID_ROOT"].get("Target") == "ALC_UNRAID_ROOT"
    assert entries["ALC_UNRAID_ROOT"].get("Default") in {"", "/unraid/mnt"}


def test_the_language_variable_sets_the_locale(root):
    """S1e (#47): the template carries ALC_LOCALE with the other variables."""
    entries = configs(root)
    language = entries["ALC_LOCALE"]
    assert language.get("Name") == "Language"
    assert language.get("Type") == "Variable"
    # Empty by default, so the language chosen in Settings applies (#47).
    assert language.get("Default") == ""
    assert (language.text or "").strip() == ""
    assert language.get("Display") == "always"
    assert language.get("Required") == "false"
    assert language.get("Mask") == "false"
    assert "en, ca or es" in language.get("Description")
    assert "Settings" in language.get("Description")
    # Every value the template offers is a supported locale.
    from almali_library_curator.i18n import SUPPORTED_LOCALES

    for code in ("en", "ca", "es"):
        assert code in SUPPORTED_LOCALES
        assert code in language.get("Description")
