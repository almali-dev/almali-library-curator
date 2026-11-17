"""#71: the owner password can come from a file or a hash, never only the environment.

Everything goes through the public interface: the environment the app reads,
the login form and the log lines. The plain ``ALC_AUTH_PASSWORD`` keeps
working; a file and a hash are the two new sources, and a password is never
kept in clear text beside the screens or in ``settings.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re

import pytest
from werkzeug.security import generate_password_hash

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings


PASSWORD = "file-backed owner password 91bd"


def build(settings):
    app = create_app(Settings(config_dir=settings.config_dir, scheduler_enabled=False))
    app.config.update(TESTING=True)  # CSRF stays enabled, as in production
    return app


def login(client, password=PASSWORD):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return client.post("/login", data={
        "user": os.environ["ALC_AUTH_USER"], "password": password, "csrf_token": token})


def password_file(tmp_path, password=PASSWORD, mode=0o600, name="auth-password"):
    path = tmp_path / name
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def without_plain(monkeypatch):
    monkeypatch.delenv("ALC_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("ALC_AUTH_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("ALC_AUTH_PASSWORD_HASH", raising=False)


def events(caplog):
    return [record.getMessage() for record in caplog.records]


# -- a password file ---------------------------------------------------------


def test_a_password_file_signs_the_owner_in(settings, tmp_path, monkeypatch):
    without_plain(monkeypatch)
    path = password_file(tmp_path)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_FILE", str(path))
    app = build(settings)
    client = app.test_client()
    assert login(client).status_code == 303
    # The reverse proves it is the file that authenticated, not a length guess.
    assert login(app.test_client(), password=path.read_text().strip() + "x").status_code == 401
    # The secret is not copied into the config directory nor into settings.
    for stored in settings.config_dir.rglob("*"):
        if stored.is_file():
            assert PASSWORD.encode() not in stored.read_bytes(), stored
    saved = json.loads((settings.config_dir / "settings.json").read_text())
    assert not [key for key in saved if "auth" in key]


def test_a_trailing_newline_in_the_password_file_is_not_part_of_the_password(
        settings, tmp_path, monkeypatch):
    without_plain(monkeypatch)
    path = password_file(tmp_path)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_FILE", str(path))
    assert login(build(settings).test_client()).status_code == 303
    assert login(build(settings).test_client(), password=PASSWORD + "\n").status_code == 401


def test_a_missing_password_file_fails_closed(settings, tmp_path, monkeypatch, caplog):
    without_plain(monkeypatch)
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_FILE", str(tmp_path / "missing"))
    app = build(settings)
    client = app.test_client()
    assert client.get("/").status_code == 503
    assert client.get("/login").status_code == 503
    assert client.post("/login", data={
        "user": os.environ["ALC_AUTH_USER"], "password": PASSWORD}).status_code == 503
    assert "auth_password_file_unreadable" in events(caplog)


def test_a_world_readable_password_file_is_reported(settings, tmp_path, monkeypatch, caplog):
    without_plain(monkeypatch)
    caplog.set_level(logging.WARNING)
    path = password_file(tmp_path, mode=0o644)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_FILE", str(path))
    app = build(settings)
    assert "auth_password_file_permissions" in events(caplog)
    assert login(app.test_client()).status_code == 303
    # The report never carries the secret.
    assert all(PASSWORD not in str(vars(record)) for record in caplog.records)


# -- a password hash ---------------------------------------------------------


def test_a_password_hash_signs_the_owner_in(settings, monkeypatch):
    without_plain(monkeypatch)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_HASH", generate_password_hash(PASSWORD))
    app = build(settings)
    assert login(app.test_client()).status_code == 303
    # The hash itself is never a valid password.
    assert login(app.test_client(), password=os.environ["ALC_AUTH_PASSWORD_HASH"]).status_code == 401


def test_an_invalid_password_hash_fails_closed(settings, monkeypatch, caplog):
    without_plain(monkeypatch)
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_HASH", "not-a-valid-hash")
    app = build(settings)
    client = app.test_client()
    assert client.get("/").status_code == 503
    assert client.post("/login", data={
        "user": os.environ["ALC_AUTH_USER"], "password": PASSWORD}).status_code == 503
    assert "auth_password_hash_invalid" in events(caplog)


def test_the_hash_wins_over_a_plain_password_left_in_the_environment(settings, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_HASH", generate_password_hash(PASSWORD))
    monkeypatch.setenv("ALC_AUTH_PASSWORD", "leftover plain password")
    app = build(settings)
    assert login(app.test_client()).status_code == 303
    assert login(app.test_client(), password="leftover plain password").status_code == 401
    assert "auth_password_sources_conflict" in events(caplog)


def test_rotating_the_hash_signs_the_old_session_out(settings, monkeypatch):
    without_plain(monkeypatch)
    monkeypatch.setenv("ALC_AUTH_PASSWORD_HASH", generate_password_hash(PASSWORD))
    first = build(settings)
    client = first.test_client()
    response = login(client)
    assert response.status_code == 303
    cookie = next(value for value in response.headers.getlist("Set-Cookie")
                  if value.startswith("alc_session="))
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    # Same signing key, a new password: the old cookie must not open the app.
    monkeypatch.setenv("ALC_AUTH_PASSWORD_HASH", generate_password_hash(PASSWORD + "-rotated"))
    second = build(settings)
    stale = second.test_client()
    stale.set_cookie("alc_session", value)
    assert stale.get("/").status_code == 303
    assert stale.get("/").headers["Location"].startswith("/login")


# -- a short password --------------------------------------------------------


def test_a_short_password_is_reported_but_still_signs_in(settings, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("ALC_AUTH_PASSWORD", "abc12")
    app = build(settings)
    assert login(app.test_client(), password="abc12").status_code == 303
    record = next(item for item in caplog.records
                  if item.getMessage() == "auth_password_short")
    assert record.min_length >= 8
    assert "abc12" not in caplog.text


def test_a_long_enough_plain_password_is_not_reported(settings, caplog):
    caplog.set_level(logging.WARNING)
    build(settings).test_client()
    assert "auth_password_short" not in events(caplog)


# -- the existing guard stays -------------------------------------------------


@pytest.mark.parametrize("name", ["ALC_AUTH_PASSWORD", "ALC_AUTH_PASSWORD_FILE", "ALC_AUTH_PASSWORD_HASH"])
def test_a_blank_source_never_authenticates(settings, monkeypatch, name):
    without_plain(monkeypatch)
    monkeypatch.setenv(name, "   ")
    app = build(settings)
    assert app.test_client().get("/").status_code == 503
