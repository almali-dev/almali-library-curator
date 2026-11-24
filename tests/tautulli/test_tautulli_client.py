from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_tautulli_server import (
    FakeTautulliDataset,
    FakeTautulliServer,
    history_row,
    tautulli_user,
)
from almali_library_curator.tautulli import (
    TautulliAuthError,
    TautulliClient,
    TautulliError,
    TautulliUnavailableError,
)

SECRET = "super-secret-api-key"


@pytest.fixture()
def dataset() -> FakeTautulliDataset:
    return FakeTautulliDataset(api_key=SECRET)


@pytest.fixture()
def server(dataset):
    with FakeTautulliServer(dataset) as running:
        yield running


def build_client(server, **overrides) -> TautulliClient:
    options = {
        "base_url": server.base_url,
        "api_key": SECRET,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return TautulliClient(**options)


def test_history_walks_every_page(server, dataset):
    dataset.history = [
        history_row(row_id=index, rating_key=100 + index, user_id=1, user="Marc")
        for index in range(60)
    ]

    client = build_client(server, page_size=25)
    entries = client.history()

    assert len(entries) == 60
    history_calls = server.requests_for("get_history")
    assert len(history_calls) == 3
    assert [call["body"]["start"] for call in history_calls] == ["0", "25", "50"]
    assert entries[0].rating_key == 100
    assert entries[0].user == "Marc"
    assert entries[0].full_title.startswith("Show - S01E")
    assert entries[0].started == 1711929600
    assert entries[0].stopped == 1711932300
    assert entries[0].paused_counter == 0


def test_history_forwards_the_user_and_date_filters(server, dataset):
    dataset.history = [history_row(1, 100, 1, "Marc")]

    build_client(server).history(user_id=1, after="2024-01-01")

    body = server.requests_for("get_history")[0]["body"]
    assert body["user_id"] == "1"
    assert body["after"] == "2024-01-01"


def test_an_empty_string_is_no_value_in_the_optional_numbers_of_a_row():
    """#125: Tautulli sends '' where a movie row has no number to report."""
    from almali_library_curator.tautulli import TautulliHistoryEntry

    optional_numbers = [
        name for name, info in TautulliHistoryEntry.model_fields.items()
        if info.annotation in (int | None, "Optional[int]")
    ]
    assert "media_index" in optional_numbers and "parent_media_index" in optional_numbers
    for name in optional_numbers:
        row = history_row(1, 100, 1, "Marc")
        row[name] = ""
        entry = TautulliHistoryEntry.model_validate(row)
        assert getattr(entry, name) is None, name


def test_users_returns_typed_entries(server, dataset):
    dataset.users = [tautulli_user(1, "Marc"), tautulli_user(2, "Anna")]

    users = build_client(server).users()

    assert [entry.username for entry in users] == ["Marc", "Anna"]
    assert users[0].user_id == 1
    assert users[0].email == "marc@example.com"
    assert users[1].is_admin == 0


def test_the_api_key_travels_as_a_body_parameter_never_the_url(server, dataset):
    dataset.users = [tautulli_user(1, "Marc")]

    build_client(server).users()

    request = server.requests_for("get_users")[0]
    assert request["method"] == "POST"
    assert request["body"]["apikey"] == SECRET
    assert "apikey" not in request["query"]
    assert SECRET not in request["path"]
    # No request the client made ever carries the key in a URL part, where a
    # log record or an error message could pick it up.
    for recorded in server.requests:
        assert SECRET not in recorded["path"]
        assert SECRET not in recorded["query"]


def test_the_api_key_never_reaches_an_error_or_a_log_record(server, dataset, caplog):
    dataset.fail_always("get_users", status=503)
    client = build_client(server, max_retries=0)

    with pytest.raises(TautulliUnavailableError) as failure:
        client.users()

    assert SECRET not in str(failure.value)
    assert SECRET not in caplog.text


def test_rejected_credentials_raise_an_auth_error_without_retrying(server, dataset):
    client = build_client(server, api_key="wrong-api-key")

    with pytest.raises(TautulliAuthError) as failure:
        client.users()

    assert "wrong-api-key" not in str(failure.value)
    assert len(server.requests_for("get_users")) == 1


def test_a_transient_503_is_retried_once_then_succeeds(server, dataset):
    dataset.users = [tautulli_user(1, "Marc")]
    dataset.fail_once("get_users", status=503)
    client = build_client(server, max_retries=2)

    users = client.users()

    assert [entry.username for entry in users] == ["Marc"]
    assert len(server.requests_for("get_users")) == 2


def test_an_error_envelope_with_http_200_still_raises(server, dataset):
    dataset.fail_command("get_history", "Not a valid date format.")

    with pytest.raises(TautulliError) as failure:
        build_client(server).history()

    assert "Not a valid date format" in str(failure.value)
    assert SECRET not in str(failure.value)


def test_a_redirect_is_refused_and_never_followed(server, dataset):
    dataset.redirects["get_users"] = "http://127.0.0.1:9/steal-the-api-key"

    with pytest.raises(TautulliError) as failure:
        build_client(server).users()

    assert not isinstance(failure.value, TautulliUnavailableError)
    assert "redirect" in str(failure.value)
    assert SECRET not in str(failure.value)
    assert len(server.requests_for("get_users")) == 1


def test_from_settings_builds_the_client_from_the_settings_attributes(server, dataset):
    dataset.users = [tautulli_user(1, "Marc")]
    settings = SimpleNamespace(
        tautulli_base_url=server.base_url, tautulli_api_key=SECRET
    )

    client = TautulliClient.from_settings(
        settings, rate_limit_per_second=1000.0, backoff_seconds=0.0
    )

    assert SECRET not in repr(client)
    assert [entry.username for entry in client.users()] == ["Marc"]
