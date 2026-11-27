"""Regression coverage for the v0.22.0 quick UI fixes (issues #21 and #24–29)."""
from __future__ import annotations

from html import unescape
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.i18n import translate
from almali_library_curator.web import actions, routes
from almali_library_curator.web.models import PlexServerOptionView, PlexSignInView
from lifecycle_helpers import seed_library


CATALOGS = {
    locale: json.loads(
        (Path(__file__).resolve().parents[2] / f"almali_library_curator/i18n/{locale}.json").read_text()
    )
    for locale in ("en", "ca", "es")
}


def test_first_run_setup_link_uses_the_button_touch_target(client):
    html = client.get("/").get_data(as_text=True)

    assert '<a class="btn" href="/setup' in html


@pytest.mark.parametrize("locale", ("en", "ca", "es"))
def test_plex_server_picker_has_a_translated_legend(client, monkeypatch, locale):
    monkeypatch.setattr(
        routes,
        "plex_signin_view",
        lambda state: PlexSignInView(
            state="authorized",
            servers=(PlexServerOptionView("Home server", "http://plex.example:32400"),),
        ),
    )

    html = client.get(f"/connections?lang={locale}").get_data(as_text=True)
    picker = re.search(r'<fieldset class="form-fieldset">(.*?)</fieldset>', html, re.S)

    assert picker
    assert CATALOGS[locale]["connections.plex.signin.server_legend"] in picker.group(1)
    assert "<legend" in picker.group(1)


@pytest.mark.parametrize("locale", ("en", "ca", "es"))
def test_disabled_protection_explains_that_a_library_must_be_curated(app, client, conn, locale):
    seed_library(conn, "1", "Movies")
    seed_library(conn, "2", "Archive")
    save_settings(replace(app.config["SETTINGS"], curated_libraries=("1",)))

    for path in ("/settings", "/setup?step=libraries"):
        html = client.get(f"{path}{'&' if '?' in path else '?'}lang={locale}").get_data(as_text=True)
        scopes = re.findall(r'<div class="library-scope">(.*?)</div>', html, re.S)
        scope = next(scope for scope in scopes if 'name="protected_libraries" value="2"' in scope)

        assert "disabled" in scope
        assert CATALOGS[locale]["settings.library_protected_requires_curated"] in unescape(scope)


@pytest.mark.parametrize("locale", ("ca", "es"))
def test_category_filter_and_settings_threshold_use_the_same_terms(locale):
    category_to_threshold = {
        "stalled_series": "stalled_series_months",
        "never_watched": "never_watched_months",
        "cancelled_no_viewers": "cancelled_no_viewers_months",
        "seasons_everyone_finished": "everyone_finished_months",
        "running_series_finished_seasons": "running_series_finished_seasons_months",
        "movies_not_rewatched": "movies_not_rewatched_months",
    }

    for category, threshold in category_to_threshold.items():
        assert translate(f"category.{category}", locale) == translate(
            f"settings.threshold.{threshold}", locale
        )


def test_spanish_protected_navigation_uses_the_plural_term():
    assert translate("nav.protected", "es") == "Protegidos"


def test_catalan_skip_link_uses_standard_central_catalan():
    assert translate("nav.skip", "ca") == "Vés al contingut"


def test_plex_signin_link_opens_in_a_new_tab_without_losing_rel_protection(app, client):
    state_path = app.config["SETTINGS"].config_dir / actions.SIGNIN_FILE_NAME
    state_path.write_text(json.dumps({
        "state": "pending",
        "code": "ABCD",
        "client_identifier": "test-client",
    }))

    html = client.get("/connections").get_data(as_text=True)
    link = re.search(r'<a class="btn" href="[^"]*plex\.tv[^"]*"([^>]*)>', html)

    assert link
    assert 'target="_blank"' in link.group(1)
    assert 'rel="noopener noreferrer"' in link.group(1)


@pytest.mark.parametrize("locale", ("en", "ca", "es"))
def test_settings_offer_the_running_series_threshold_in_the_reader_language(client, locale):
    """#135: its own threshold, with its own label and help, in every catalog."""
    html = unescape(client.get(f"/settings?lang={locale}").get_data(as_text=True))
    label = CATALOGS[locale]["settings.threshold.running_series_finished_seasons_months"]
    help_text = CATALOGS[locale]["settings.help.running_series_finished_seasons_months"]

    assert 'name="running_series_finished_seasons_months"' in html
    assert label in html
    assert help_text in html
