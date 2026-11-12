"""The plex.tv PIN flow client (S13, T9c).

Every call goes to a real fake plex.tv over HTTP. The PIN code is not a
credential and may be shown; the auth token never appears in a URL.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from fake_plex_tv_server import FakePlexTvDataset, FakePlexTvServer
from almali_library_curator.plex import auth


@pytest.fixture()
def dataset() -> FakePlexTvDataset:
    return FakePlexTvDataset()


@pytest.fixture()
def plex_tv(dataset):
    with FakePlexTvServer(dataset) as running:
        yield running


def fast(plex_tv, monkeypatch):
    """Point the module at the fake and lift its shared rate limit."""
    monkeypatch.setattr(auth, "PLEX_TV_BASE_URL", plex_tv.base_url)
    monkeypatch.setattr(auth, "DEFAULT_RATE_LIMIT_PER_SECOND", 10000.0)


# -- rule 1: creating the PIN --------------------------------------------------


def test_create_pin_posts_to_plex_tv_with_the_client_headers(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    pin = auth.create_pin()
    request = plex_tv.pin_creations[0]
    assert request["method"] == "POST"
    assert request["headers"]["X-Plex-Client-Identifier"] == auth.CLIENT_IDENTIFIER
    assert request["headers"]["X-Plex-Product"] == auth.PRODUCT_NAME
    assert pin.code == dataset.pins[pin.id]["code"]


def test_the_pin_is_requested_without_strong_so_the_token_is_long_lived(plex_tv, monkeypatch):
    """S13 rule 2: never ``strong=true``; the seven-day JWT would expire
    under the daily scan schedule."""
    fast(plex_tv, monkeypatch)
    auth.create_pin()
    request = plex_tv.pin_creations[0]
    assert "strong" not in request["query"]
    assert not any(
        key.lower() == "strong" for key in (request["body"] or {})
    ), request["body"]
    assert "strong" not in request["path"]


# -- rule 1: polling the PIN ---------------------------------------------------


def test_poll_pin_returns_none_until_the_token_appears(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    dataset.authorize_after_polls = 2
    pin = auth.create_pin()
    assert auth.poll_pin(pin.id) is None
    assert auth.poll_pin(pin.id) == dataset.token


def test_poll_pin_sends_the_client_headers_and_no_token(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    pin = auth.create_pin()
    auth.poll_pin(pin.id)
    request = [r for r in plex_tv.requests if r["path"] == f"/api/v2/pins/{pin.id}"][0]
    assert request["method"] == "GET"
    assert request["headers"]["X-Plex-Client-Identifier"] == auth.CLIENT_IDENTIFIER
    assert "X-Plex-Token" not in request["headers"]
    assert dataset.token not in request["path"] and "token" not in request["query"]


def test_the_authorization_link_carries_the_client_id_the_code_and_the_product_context(monkeypatch):
    """Plex needs the product context to identify the app that asks for access."""
    link = auth.authorization_link("almali-client-id", "AB12CD")
    assert link.startswith("https://app.plex.tv/auth#?")
    assert "clientID=almali-client-id" in link
    assert "code=AB12CD" in link
    assert quote(f"context[device][product]={auth.PRODUCT_NAME}", safe="") in link


# -- rule 3: the resources list ------------------------------------------------


def test_list_resources_returns_the_owned_servers_with_local_addresses(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    options = auth.list_resources(dataset.token)
    assert [(option.name, option.address) for option in options] == [
        ("Tower", "http://192.168.1.20:32400"),
        ("Backup", "http://192.168.1.21:32400"),
    ]


def test_list_resources_sends_the_token_in_the_header_never_the_url(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    dataset.required_token = dataset.token
    auth.list_resources(dataset.token)
    request = plex_tv.requests_for("/api/v2/resources")[0]
    assert request["headers"]["X-Plex-Token"] == dataset.token
    assert dataset.token not in request["path"] and not request["query"]


def test_list_resources_with_a_refused_token_raises_its_own_error(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    dataset.required_token = "another-account-token-0000"
    with pytest.raises(auth.PlexTvRefusedError):
        auth.list_resources(dataset.token)


# -- rule 4: failures are typed ------------------------------------------------


def test_a_refused_pin_creation_raises_its_own_error(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    dataset.refuse_create = 401
    with pytest.raises(auth.PlexTvRefusedError):
        auth.create_pin()


def test_an_expired_pin_raises_its_own_error(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    pin = auth.create_pin()
    dataset.expire(pin.id)
    with pytest.raises(auth.PlexPinExpiredError):
        auth.poll_pin(pin.id)


def test_an_unknown_pin_is_reported_as_expired(plex_tv, monkeypatch):
    fast(plex_tv, monkeypatch)
    with pytest.raises(auth.PlexPinExpiredError):
        auth.poll_pin("999999")


def test_an_unreachable_plex_tv_raises_its_own_error(plex_tv, monkeypatch):
    monkeypatch.setattr(auth, "PLEX_TV_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(auth.PlexTvUnavailableError):
        auth.create_pin()


def test_a_plex_tv_server_error_is_reported_as_unreachable(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    dataset.fail_always["/api/v2/pins"] = 503
    with pytest.raises(auth.PlexTvUnavailableError):
        auth.create_pin()


def test_a_redirect_from_plex_tv_is_never_followed(plex_tv, dataset, monkeypatch):
    """The token header must never be re-sent to another host (the T2
    redirect lesson, applied to plex.tv)."""
    fast(plex_tv, monkeypatch)
    dataset.redirects["/api/v2/resources"] = "http://elsewhere.invalid/"
    with pytest.raises(auth.PlexTvError):
        auth.list_resources(dataset.token)
    assert len(plex_tv.requests) == 1


def test_a_pin_without_an_id_or_code_is_not_a_pin(plex_tv, dataset, monkeypatch):
    fast(plex_tv, monkeypatch)
    monkeypatch.setattr(dataset, "create_pin", lambda: {"id": None, "code": "X"})
    with pytest.raises(auth.PlexTvUnavailableError):
        auth.create_pin()


def test_list_resources_without_a_token_is_refused(plex_tv, monkeypatch):
    fast(plex_tv, monkeypatch)
    with pytest.raises(auth.PlexTvRefusedError):
        auth.list_resources("")


def test_every_auth_error_is_a_plex_tv_error(plex_tv, monkeypatch):
    fast(plex_tv, monkeypatch)
    assert issubclass(auth.PlexTvRefusedError, auth.PlexTvError)
    assert issubclass(auth.PlexPinExpiredError, auth.PlexTvError)
    assert issubclass(auth.PlexTvUnavailableError, auth.PlexTvError)


# -- the rate limit the Plex client already uses -------------------------------


def test_plex_tv_calls_share_the_rate_limit(monkeypatch):
    seen = []
    fake_clock = iter([0.0, 0.01, 0.02, 0.03, 0.04]).__next__

    def fake_sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr(auth, "_monotonic", fake_clock)
    monkeypatch.setattr(auth, "_sleep", fake_sleep)
    limiter = auth._Throttle(rate_limit_per_second=5.0)
    limiter.wait()
    limiter.wait()
    limiter.wait()
    # The second and third call each wait for the rest of their slot.
    assert seen == [0.19, 0.19]


def test_a_disabled_rate_limit_never_sleeps(monkeypatch):
    def fail_sleep(seconds):
        raise AssertionError("sleep must not be called")

    monkeypatch.setattr(auth, "_sleep", fail_sleep)
    auth._Throttle(rate_limit_per_second=0).wait()
    auth._Throttle(rate_limit_per_second=0).wait()
