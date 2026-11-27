"""One image from Plex, over the wire (#139, S3).

The same server fake the poster route's tests use, so the bytes, the content
type, the token header and the cap are checked against a real HTTP answer
instead of a mock of this app's own code. S5's fail-fast rule is checked here
too: an image read makes one attempt by default, and a JSON read does not
change.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from fake_plex_server import FakePlexDataset, FakePlexServer  # noqa: E402
from poster_fakes import (  # noqa: E402
    JPEG, OVER_CAP, PNG, THUMB, TRANSCODER, FakePosterPlexServer,
)

from almali_library_curator.plex import (  # noqa: E402
    PlexAuthError, PlexClient, PlexError, PlexNotFoundError, PlexUnavailableError,
)

TOKEN = "plex-image-token-3a7c9d"


class RecordingSession(requests.Session):
    """A real session that keeps the keyword arguments of every request."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    def request(self, method, url, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return super().request(method, url, **kwargs)


@pytest.fixture()
def server():
    with FakePosterPlexServer(token=TOKEN) as running:
        running.serve()
        yield running


def build_client(server, **overrides) -> PlexClient:
    options = {
        "base_url": server.base_url,
        "token": TOKEN,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
        "max_retries": 0,
    }
    options.update(overrides)
    return PlexClient(**options)


def test_the_image_bytes_and_its_content_type_come_back(server):
    body, content_type = build_client(server).image(TRANSCODER, {"url": THUMB})

    assert body == PNG
    assert content_type == "image/png"


def test_the_request_asks_for_an_image_and_posts_the_token_as_a_header(server):
    build_client(server).image(TRANSCODER, {"url": THUMB})

    request = server.requests[-1]
    assert request["path"] == TRANSCODER
    assert request["headers"]["Accept"] == "image/*"
    assert request["headers"]["X-Plex-Token"] == TOKEN
    # Never in the URL, neither as a parameter nor as part of the path.
    assert TOKEN not in request["raw_path"]
    assert "x-plex-token" not in request["raw_path"].lower()


def test_the_parameters_are_encoded_into_the_query(server):
    build_client(server).image(TRANSCODER, {"url": THUMB, "width": "240"})

    request = server.requests[-1]
    assert request["query"] == {"url": THUMB, "width": "240"}
    assert "url=%2Flibrary%2Fmetadata%2F11%2Fthumb%2F1700000000" in request["raw_path"]


def test_a_parameter_on_the_content_type_is_dropped(server):
    server.serve(TRANSCODER, content_type="image/jpeg; charset=binary", body=JPEG)

    body, content_type = build_client(server).image(TRANSCODER)

    assert body == JPEG
    assert content_type == "image/jpeg"


def test_an_answer_without_a_content_type_comes_back_empty(server):
    server.images[TRANSCODER] = ("", JPEG)

    _, content_type = build_client(server).image(TRANSCODER)

    assert content_type == ""


def test_a_path_plex_does_not_have_is_a_not_found_error(server):
    with pytest.raises(PlexNotFoundError):
        build_client(server).image("/library/metadata/77/thumb/1")


def test_a_server_error_is_an_unavailable_error(server):
    server.statuses[TRANSCODER] = 500

    with pytest.raises(PlexUnavailableError):
        build_client(server).image(TRANSCODER)


def test_a_redirect_is_refused_and_never_followed(server):
    server.redirects[TRANSCODER] = "/library/metadata/99/thumb/1"

    with pytest.raises(PlexError):
        build_client(server).image(TRANSCODER)

    assert server.paths == [TRANSCODER]


def test_rejected_credentials_are_an_auth_error_without_the_token(server):
    with pytest.raises(PlexAuthError) as raised:
        build_client(server, token="refused-token-77").image(TRANSCODER)

    assert "refused-token-77" not in str(raised.value)


def test_a_declared_body_over_the_cap_is_refused(server):
    server.images[TRANSCODER] = ("image/png", OVER_CAP)

    with pytest.raises(PlexError) as raised:
        build_client(server).image(TRANSCODER)

    assert TOKEN not in str(raised.value)


def test_a_streamed_body_over_the_cap_is_refused(server):
    # No Content-Length: the client has to count the bytes it reads.
    server.no_content_length = True
    server.images[TRANSCODER] = ("image/png", OVER_CAP)

    with pytest.raises(PlexError):
        build_client(server).image(TRANSCODER)


# -- S5 fail fast (L5): one attempt, a short timeout, and JSON unchanged -----

def test_an_image_makes_one_attempt_by_default(server):
    server.fail_all = 500
    client = build_client(server, max_retries=3, backoff_seconds=0.0)

    with pytest.raises(PlexUnavailableError):
        client.image(TRANSCODER)

    # One request, not the four a JSON read would make.
    assert server.paths == [TRANSCODER]


def test_an_image_read_uses_the_timeout_it_is_given(server):
    session = RecordingSession()
    client = build_client(server, session=session, timeout=15.0)

    client.image(TRANSCODER, {"url": THUMB}, timeout=5.0)

    assert session.timeouts == [5.0]


def test_a_json_read_still_retries_as_before():
    dataset = FakePlexDataset()
    # A server that keeps failing is one request plus the client's retries.
    dataset.always_fail["/library/sections"] = 503
    with FakePlexServer(dataset) as server:
        client = PlexClient(base_url=server.base_url, token=TOKEN,
                            rate_limit_per_second=1000.0, backoff_seconds=0.0)

        with pytest.raises(PlexUnavailableError):
            client.sections()

        assert len(server.requests_for("/library/sections")) == 4


def test_a_json_read_that_fails_once_recovers_on_its_retry():
    dataset = FakePlexDataset()
    dataset.fail_once("/library/sections", 503)
    with FakePlexServer(dataset) as server:
        client = PlexClient(base_url=server.base_url, token=TOKEN,
                            rate_limit_per_second=1000.0, backoff_seconds=0.0)

        assert client.sections()

        assert len(server.requests_for("/library/sections")) == 2
