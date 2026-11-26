"""S3 (#115): an invalid ALC_AUTH_PASSWORD_HASH stays closed and says how to recover.

The owner who switches back to the plain password without removing the hash
cannot sign in. Failing closed is right and stays: what is added is that the
startup log and the page the owner lands on both name
``ALC_AUTH_PASSWORD_HASH`` as the invalid variable, give the recovery and
never show the hash, the password or any other secret, in en, ca and es.

Everything goes through the public interface: the environment the app reads,
the Flask test client, the rendered page and the captured log records.
"""
from __future__ import annotations

import logging
import os
import re

from markupsafe import Markup
import pytest
from werkzeug.security import generate_password_hash

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings
from almali_library_curator.i18n import translate

HASH_VARIABLE = "ALC_AUTH_PASSWORD_HASH"
FILE_VARIABLE = "ALC_AUTH_PASSWORD_FILE"
PLAIN_VARIABLE = "ALC_AUTH_PASSWORD"
LOCALES = ("en", "ca", "es")

# A valid plain password and a value that is not a Werkzeug hash at all; both
# are unique enough to grep the page and the log for a leak.
PLAIN = "plain owner password 7b31"
INVALID_HASH = "not-a-werkzeug-hash-5e27"

# The recovery wording, per locale: name the variable, say fix it or remove
# it so the plain password or the file is used, then restart the container.
RECOVERY_WORDS = {
    "en": ("not a valid", "fix", "remove", "restart"),
    "ca": ("no és un hash de contrasenya de werkzeug vàlid", "corregeix", "elimina", "reinicia"),
    "es": ("no es un hash de contraseña de werkzeug válido", "corrige", "elimina", "reinicia"),
}


def build(settings):
    app = create_app(Settings(config_dir=settings.config_dir, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    return app


def text(response):
    return " ".join(Markup(response.get_data(as_text=True)).striptags().split())


def said(key, locale="en"):
    return " ".join(Markup(translate(key, locale)).striptags().split())


def owner_login(client, password):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    data = {"user": os.environ["ALC_AUTH_USER"], "password": password}
    if token:
        data["csrf_token"] = token.group(1)
    return client.post("/login", data=data)


def invalid_hash(monkeypatch):
    monkeypatch.setenv(PLAIN_VARIABLE, PLAIN)
    monkeypatch.setenv(HASH_VARIABLE, INVALID_HASH)


# -- rule a: an invalid hash fails closed, even beside a valid plain password --


def test_an_invalid_hash_refuses_the_valid_plain_password(settings, monkeypatch):
    """The hash wins over the plain password, so the valid one must not open."""
    invalid_hash(monkeypatch)
    app = build(settings)
    client = app.test_client()
    assert owner_login(client, PLAIN).status_code == 503
    assert client.get("/").status_code == 503
    assert client.get("/settings").status_code == 503


def test_the_same_plain_password_signs_in_once_the_invalid_hash_is_removed(
        settings, monkeypatch):
    """The reverse control: it is the leftover hash that closes the app."""
    monkeypatch.setenv(PLAIN_VARIABLE, PLAIN)
    monkeypatch.delenv(HASH_VARIABLE, raising=False)
    assert owner_login(build(settings).test_client(), PLAIN).status_code == 303


# -- rule b: the startup log names the variable and the recovery, no secret ---


def test_the_startup_log_names_the_invalid_variable_and_the_recovery(
        settings, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    invalid_hash(monkeypatch)
    build(settings)
    record = next(item for item in caplog.records
                  if item.getMessage() == "auth_password_hash_invalid")
    assert HASH_VARIABLE in str(record.__dict__.get("variable", ""))
    recovery = str(record.__dict__.get("recovery", ""))
    assert HASH_VARIABLE in recovery
    assert FILE_VARIABLE in recovery and PLAIN_VARIABLE in recovery
    for phrase in RECOVERY_WORDS["en"][1:]:
        assert phrase in recovery.lower(), phrase


def test_the_startup_log_carries_no_secret(settings, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    invalid_hash(monkeypatch)
    build(settings)
    assert INVALID_HASH not in caplog.text
    assert PLAIN not in caplog.text
    for record in caplog.records:
        fields = repr(record.__dict__)
        assert INVALID_HASH not in fields, record.getMessage()
        assert PLAIN not in fields, record.getMessage()


# -- rule c: the login page shows the same recovery hint, in en, ca and es ----


@pytest.mark.parametrize("locale", LOCALES)
def test_the_login_page_names_the_invalid_variable_and_the_recovery(
        settings, monkeypatch, locale):
    invalid_hash(monkeypatch)
    response = build(settings).test_client().get(f"/login?lang={locale}")
    assert response.status_code == 503
    assert f'<html lang="{locale}"' in response.get_data(as_text=True)
    body = text(response)
    # The recovery only exists once the page names the variable it is about;
    # the app stays closed, exactly as before.
    assert HASH_VARIABLE in body
    assert FILE_VARIABLE in body
    for phrase in RECOVERY_WORDS[locale]:
        assert phrase in body.lower(), phrase
    assert said("error.auth_hash_invalid.message", locale) in body
    # No secret reaches the page.
    assert INVALID_HASH not in body
    assert PLAIN not in body


def test_the_recovery_hint_is_translated():
    """en, ca and es each say it in their own words, not the English fallback."""
    english = said("error.auth_hash_invalid.message", "en")
    assert english and english != "error.auth_hash_invalid.message"
    for locale in ("ca", "es"):
        translated = said("error.auth_hash_invalid.message", locale)
        assert translated and translated != "error.auth_hash_invalid.message"
        assert translated != english


# -- rule d: a valid configuration shows no hint ------------------------------


@pytest.mark.parametrize("source", ["hash", "plain"])
def test_a_valid_login_page_shows_no_such_hint(settings, monkeypatch, source):
    if source == "hash":
        monkeypatch.setenv(HASH_VARIABLE, generate_password_hash(PLAIN))
        monkeypatch.delenv(PLAIN_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(PLAIN_VARIABLE, PLAIN)
        monkeypatch.delenv(HASH_VARIABLE, raising=False)
    response = build(settings).test_client().get("/login")
    assert response.status_code == 200
    body = text(response)
    assert HASH_VARIABLE not in body
    assert said("error.auth_hash_invalid.message", "en") not in body
    assert said("error.auth_hash_invalid.title", "en") not in body


def test_a_password_file_login_page_shows_no_such_hint(settings, monkeypatch, tmp_path):
    monkeypatch.delenv(PLAIN_VARIABLE, raising=False)
    monkeypatch.delenv(HASH_VARIABLE, raising=False)
    path = tmp_path / "auth-password"
    path.write_text(PLAIN + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(FILE_VARIABLE, str(path))
    response = build(settings).test_client().get("/login")
    assert response.status_code == 200
    assert HASH_VARIABLE not in text(response)


# -- the guard around the message: nothing else opens ------------------------


def test_every_screen_names_the_invalid_variable_and_none_opens(settings, monkeypatch):
    """The page is the same 503 everywhere; the app never half-opens."""
    invalid_hash(monkeypatch)
    client = build(settings).test_client()
    for path in ("/", "/?tab=protected", "/history", "/settings", "/connections", "/setup"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert HASH_VARIABLE in text(response), path


def test_healthz_stays_alive_and_reports_the_missing_login(settings, monkeypatch):
    """A closed login is still a liveness answer, never a crash loop."""
    invalid_hash(monkeypatch)
    response = build(settings).test_client().get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert "auth" in payload["missing"] and payload["ready"] is False


def test_an_invalid_hash_closes_the_app_even_beside_a_password_file(
        settings, monkeypatch, tmp_path):
    """Every other source in the environment stays unused while the hash is
    there and unreadable, so the recovery is the only way back in."""
    monkeypatch.delenv(PLAIN_VARIABLE, raising=False)
    path = tmp_path / "auth-password"
    path.write_text(PLAIN + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(FILE_VARIABLE, str(path))
    monkeypatch.setenv(HASH_VARIABLE, INVALID_HASH)
    client = build(settings).test_client()
    assert owner_login(client, PLAIN).status_code == 503
    assert HASH_VARIABLE in text(client.get("/login"))


def test_no_user_name_still_gets_the_plain_configured_page(settings, monkeypatch):
    """Only the invalid hash gets the new page; a missing login keeps the old
    one, so the recovery never replaces its own contract."""
    monkeypatch.delenv("ALC_AUTH_USER", raising=False)
    response = build(settings).test_client().get("/login")
    assert response.status_code == 503
    body = text(response)
    assert said("error.auth_missing.message") in body
    assert said("error.auth_hash_invalid.message") not in body


# -- rule e: the hint does not depend on ALC_AUTH_USER being set --------------
# Advisory finding of the 0.43.0 review: with the user variable missing, the
# app answered the generic "login missing" page and logged nothing about the
# invalid hash, so the owner fixed the user and still met a closed app.


def test_without_a_user_the_startup_log_still_names_the_invalid_hash(
        settings, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    invalid_hash(monkeypatch)
    monkeypatch.delenv("ALC_AUTH_USER", raising=False)
    build(settings)
    record = next((item for item in caplog.records
                   if item.getMessage() == "auth_password_hash_invalid"), None)
    assert record is not None
    assert HASH_VARIABLE in str(record.__dict__.get("recovery", ""))
    assert INVALID_HASH not in caplog.text and PLAIN not in caplog.text


@pytest.mark.parametrize("locale", LOCALES)
def test_without_a_user_the_login_page_still_shows_the_recovery(
        settings, monkeypatch, locale):
    invalid_hash(monkeypatch)
    monkeypatch.delenv("ALC_AUTH_USER", raising=False)
    response = build(settings).test_client().get(f"/login?lang={locale}")
    assert response.status_code == 503
    body = text(response)
    assert said("error.auth_hash_invalid.message", locale) in body
    assert INVALID_HASH not in body and PLAIN not in body


def test_without_a_user_and_without_a_hash_the_missing_login_page_is_unchanged(
        settings, monkeypatch):
    monkeypatch.setenv(PLAIN_VARIABLE, PLAIN)
    monkeypatch.delenv(HASH_VARIABLE, raising=False)
    monkeypatch.delenv("ALC_AUTH_USER", raising=False)
    response = build(settings).test_client().get("/login")
    assert response.status_code == 503
    body = text(response)
    assert said("error.auth_missing.message", "en") in body
    assert said("error.auth_hash_invalid.message", "en") not in body
