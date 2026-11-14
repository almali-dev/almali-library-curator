"""S15 rules 4 and 7: the Plex delete request and its error classes."""

from __future__ import annotations

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from almali_library_curator.plex import PlexAuthError, PlexClient, PlexError, PlexUnavailableError


def client_for(server, **overrides):
    options = {"base_url": server.base_url, "token": "test-token",
               "rate_limit_per_second": 0, "backoff_seconds": 0.0, "max_retries": 0}
    options.update(overrides)
    return PlexClient(**options)


def test_a_delete_answered_with_an_empty_body_succeeds():
    from almali_library_curator.plex.deletion import delete_media_item

    dataset = FakePlexDataset()
    dataset.metadata["42"] = movie("42", "Any title")
    with FakePlexServer(dataset) as server:
        assert delete_media_item(client_for(server), "42") is True
        deletes = [r for r in server.requests if r["method"] == "DELETE"]
    assert [r["path"] for r in deletes] == ["/library/metadata/42"]
    assert deletes[0]["headers"]["X-Plex-Token"] == "test-token"
    assert dataset.deleted_media == ["42"]


def test_an_item_plex_no_longer_knows_is_reported_as_already_gone():
    from almali_library_curator.plex.deletion import delete_media_item

    dataset = FakePlexDataset()
    dataset.missing_media.add("42")
    with FakePlexServer(dataset) as server:
        assert delete_media_item(client_for(server), "42") is False


def test_a_forbidden_delete_with_an_accepted_token_is_a_refusal():
    from almali_library_curator.plex.deletion import PlexDeletionRefused, delete_media_item

    dataset = FakePlexDataset()
    dataset.fail_always("/library/metadata/42", 403)
    with FakePlexServer(dataset) as server:
        with pytest.raises(PlexDeletionRefused) as raised:
            delete_media_item(client_for(server), "42")
    assert "Allow media deletion" in str(raised.value)
    assert "test-token" not in str(raised.value)
    assert not isinstance(raised.value, (PlexAuthError, PlexUnavailableError))


def test_a_rejected_token_stays_an_authentication_error():
    from almali_library_curator.plex.deletion import PlexDeletionRefused, delete_media_item

    dataset = FakePlexDataset()
    dataset.required_token = "another-token"
    with FakePlexServer(dataset) as server:
        with pytest.raises(PlexAuthError) as raised:
            delete_media_item(client_for(server), "42")
    assert not isinstance(raised.value, PlexDeletionRefused)
    assert dataset.deleted_media == []


def test_an_unreachable_server_stays_an_unavailable_error():
    from almali_library_curator.plex.deletion import delete_media_item

    dataset = FakePlexDataset()
    dataset.fail_always("/library/metadata/42", 503)
    with FakePlexServer(dataset) as server:
        with pytest.raises(PlexUnavailableError):
            delete_media_item(client_for(server), "42")


def test_a_read_still_refuses_an_empty_body():
    """The tolerant transport is opt-in: every read keeps requiring a body."""
    dataset = FakePlexDataset()
    with FakePlexServer(dataset) as server:
        client = client_for(server)
        # The fake answers the delete endpoint with an empty body; a read of
        # the same shape must still fail loudly.
        with pytest.raises(PlexError):
            client._request("/library/metadata/42", method="DELETE")
