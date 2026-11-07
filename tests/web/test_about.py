"""T14 / S19: the About page (rules 1-4).

Rule 1: ``GET /about`` sits behind the owner login and shows the product
name, the running version, the one-sentence intent of S1, the catalogue
disclaimer, the licence and the links to the repository, the changelog, the
security policy and the issue tracker. Rule 2: every signed-in screen links
to it with text. Rule 3: every new string exists in en, ca and es. Rule 4:
the page makes no outbound call and shows no secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from flask.testing import FlaskClient
from markupsafe import Markup
import pytest
import requests

from almali_library_curator import __version__
from almali_library_curator.auth import PUBLIC_ENDPOINTS
from almali_library_curator.i18n import translate

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
LOCALES = ("en", "ca", "es")
REPOSITORY = "https://github.com/almali-dev/almali-library-curator"
CHANGELOG = f"{REPOSITORY}/blob/main/CHANGELOG.md"
SECURITY = f"{REPOSITORY}/blob/main/SECURITY.md"
ISSUES = f"{REPOSITORY}/issues"
# Every key the About page adds, in the three catalogues.
ABOUT_KEYS = ("nav.about", "about.intent", "about.version", "about.licence",
              "about.licence_name", "about.links", "about.repository",
              "about.changelog", "about.security", "about.issues")


def text(response) -> str:
    """The visible text of a page, without the markup."""
    return ' '.join(Markup(response.get_data(as_text=True)).striptags().split())


def catalog(locale: str) -> dict:
    return json.loads((ROOT / f"i18n/{locale}.json").read_text())


# -- rule 1: behind the login, with the product facts ----------------------------


def test_the_about_page_is_not_a_public_route(app):
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules() if rule.rule == "/about"}

    assert endpoints == {"web.about"}
    assert not (endpoints & PUBLIC_ENDPOINTS)


def test_an_anonymous_about_page_redirects_to_the_login(app):
    response = FlaskClient(app).get("/about")

    assert response.status_code == 303
    location = urlsplit(response.headers["Location"])
    assert location.path == "/login" and not location.netloc
    assert parse_qs(location.query)["next"] == ["/about"]


def test_the_about_page_shows_the_product_the_running_version_and_the_intent(client):
    response = client.get("/about")
    page = text(response)

    assert response.status_code == 200
    assert translate("app.name", "en") in page
    assert __version__ in page
    assert translate("about.intent", "en") in page
    assert translate("footer.disclaimer", "en") in page
    assert "MIT" in page


def test_the_about_page_shows_the_running_version_of_the_app(client):
    """The version comes from the package, not from a copy in the template."""
    html = client.get("/about").get_data(as_text=True)

    assert __version__ in html
    assert re.search(rf"[^0-9]{re.escape(__version__)}[^0-9]", html)


def test_the_about_page_links_to_the_repository_the_changelog_the_security_policy_and_the_issues(client):
    html = client.get("/about").get_data(as_text=True)

    for url in (REPOSITORY, CHANGELOG, SECURITY, ISSUES):
        assert url in html, url
    for key in ("about.repository", "about.changelog", "about.security", "about.issues"):
        assert translate(key, "en") in text(client.get("/about")), key


# -- rule 2: the link from every signed-in screen ---------------------------------


@pytest.mark.parametrize("path", ["/", "/?tab=leaving_soon", "/history", "/connections",
                                  "/settings", "/setup"])
def test_every_signed_in_screen_links_to_the_about_page_with_text(client, path):
    response = client.get(path)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert re.search(r'href="/about(\?lang=[a-z]{2})?"', html), path
    assert translate("nav.about", "en") in text(response)


def test_the_about_link_keeps_the_language_of_the_reader(client):
    html = client.get("/history?lang=ca").get_data(as_text=True)

    assert 'href="/about?lang=ca"' in html


def test_the_public_login_page_does_not_advertise_the_about_page(app):
    """The About page is behind the login (S19 rule 1, S17 rule 3)."""
    html = FlaskClient(app).get("/login").get_data(as_text=True)

    assert "/about" not in html


# -- rule 3: en, ca and es --------------------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
def test_the_about_page_renders_in_every_locale(client, locale):
    response = client.get(f"/about?lang={locale}")
    page = text(response)

    assert response.status_code == 200
    assert f'<html lang="{locale}"' in response.get_data(as_text=True)
    assert translate("about.intent", locale) in page
    assert translate("footer.disclaimer", locale) in page
    assert translate("nav.about", locale) in page
    assert translate("about.links", locale) in page
    # No raw key reaches the page.
    assert not [key for key in catalog("en") if key in page]
    # The sentence is translated, not the English one in every language.
    if locale != "en":
        assert translate("about.intent", "en") not in page
        assert translate("footer.disclaimer", "en") not in page


def test_every_new_about_string_exists_in_every_locale():
    catalogs = {locale: catalog(locale) for locale in LOCALES}

    assert catalogs["en"].keys() == catalogs["ca"].keys() == catalogs["es"].keys()
    for locale, values in catalogs.items():
        for key in ABOUT_KEYS:
            assert values.get(key, "").strip(), (locale, key)
    # The three languages really differ: the copy is translated, not copied.
    assert catalogs["en"]["about.intent"] != catalogs["ca"]["about.intent"]
    assert catalogs["en"]["about.intent"] != catalogs["es"]["about.intent"]


# -- rule 4: no outbound call, no secret -------------------------------------------


def test_the_about_page_makes_no_outbound_call(app, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the About page must not call out")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    monkeypatch.setattr(requests, "request", refuse)
    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)
    # The guard is armed: any call would raise.
    with pytest.raises(AssertionError):
        requests.get("https://example.invalid/")

    response = app.test_client().get("/about")

    assert response.status_code == 200


def test_the_about_page_shows_no_secret(app, monkeypatch):
    secrets = {
        "ALC_PLEX_BASE_URL": "http://192.0.2.10:32400",
        "ALC_PLEX_TOKEN": "secret-plex-token-9f3a",
        "ALC_SONARR_API_KEY": "secret-sonarr-key-4b2c",
        "ALC_RADARR_API_KEY": "secret-radarr-key-6d1e",
        "ALC_TMDB_API_TOKEN": "secret-tmdb-token-77de",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    page = app.test_client().get("/about").get_data(as_text=True)

    for value in (*secrets.values(), os.environ["ALC_AUTH_PASSWORD"]):
        assert value not in page, value
    assert "secret-" not in page


def test_the_about_page_shows_no_setting_value(app, monkeypatch):
    """S19 rule 4: the page shows no setting value either."""
    settings = {
        "ALC_COLLECTION_NAME": "setting-collection-2c8f",
        "ALC_PROTECTED_LIBRARIES": "setting-library-51ab",
        "ALC_EXCLUDED_USERS": "setting-user-9d0e",
        "ALC_MEDIA_ROOT": "/mnt/setting-media-7b3d",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    page = app.test_client().get("/about").get_data(as_text=True)

    for value in settings.values():
        assert value not in page, value
