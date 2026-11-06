from __future__ import annotations

import re

from flask.testing import FlaskClient
import pytest

from almali_library_curator.config import Settings

# S17 rule 1: the login comes from the environment only. Every app the suite
# builds starts with these test credentials; the fail-closed tests remove them.
TEST_AUTH_USER = "owner"
TEST_AUTH_PASSWORD = "test-only owner password 7f3c"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(config_dir=tmp_path / "config")


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch):
    monkeypatch.setenv("ALC_AUTH_USER", TEST_AUTH_USER)
    monkeypatch.setenv("ALC_AUTH_PASSWORD", TEST_AUTH_PASSWORD)
    monkeypatch.delenv("ALC_SECURE_COOKIES", raising=False)


def owner_login(client, user=TEST_AUTH_USER, password=TEST_AUTH_PASSWORD, **fields):
    """Sign in through the public form, with the CSRF token the page embeds."""
    page = client.get("/login").get_data(as_text=True)
    data = {"user": user, "password": password, **fields}
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if token:
        data["csrf_token"] = token.group(1)
    return client.post("/login", data=data)


class OwnerClient(FlaskClient):
    """A test client that has signed in as the owner, like a real session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        response = owner_login(self)
        assert response.status_code == 303, response.status_code


@pytest.fixture()
def login():
    return owner_login


@pytest.fixture()
def owner_client_class():
    return OwnerClient
