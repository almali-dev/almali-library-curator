"""Tautulli history uses large pages without changing the imported rows."""

from __future__ import annotations

from types import SimpleNamespace

from fake_tautulli_server import (
    FakeTautulliDataset,
    FakeTautulliServer,
    history_row,
)
from almali_library_curator.tautulli import TautulliClient

API_KEY = "test-api-key"


def _history(count: int) -> list[dict]:
    return [
        history_row(
            row_id=index,
            rating_key=100 + index,
            user_id=1,
            user="Marc",
        )
        for index in range(count)
    ]


def _client(server: FakeTautulliServer, **overrides) -> TautulliClient:
    options = {
        "base_url": server.base_url,
        "api_key": API_KEY,
        "rate_limit_per_second": 1000.0,
        "backoff_seconds": 0.0,
    }
    options.update(overrides)
    return TautulliClient(**options)


def test_default_history_uses_large_pages_and_imports_every_row():
    dataset = FakeTautulliDataset(api_key=API_KEY)
    dataset.history = _history(1001)

    with FakeTautulliServer(dataset) as server:
        settings = SimpleNamespace(
            tautulli_base_url=server.base_url,
            tautulli_api_key=API_KEY,
        )
        client = TautulliClient.from_settings(
            settings,
            rate_limit_per_second=1000.0,
            backoff_seconds=0.0,
        )
        entries = client.history()
        calls = server.requests_for("get_history")

    assert client.page_size == 1000
    assert len(entries) == 1001
    assert [entry.rating_key for entry in entries[:2]] == [100, 101]
    assert [entry.rating_key for entry in entries[-2:]] == [1099, 1100]
    assert len(calls) == 2
    assert {call["body"]["length"] for call in calls} == {"1000"}
    assert [call["body"]["start"] for call in calls] == ["0", "1000"]


def test_default_history_keeps_paging_until_the_final_partial_page():
    dataset = FakeTautulliDataset(api_key=API_KEY)
    dataset.history = _history(1001)

    with FakeTautulliServer(dataset) as server:
        entries = _client(server).history()
        calls = server.requests_for("get_history")

    assert len(entries) == 1001
    assert len(calls) == 2
    assert calls[-1]["body"]["start"] == "1000"


def test_default_history_keeps_transient_failure_retries_unchanged():
    dataset = FakeTautulliDataset(api_key=API_KEY)
    dataset.history = _history(1001)
    dataset.fail_once("get_history", status=503)

    with FakeTautulliServer(dataset) as server:
        entries = _client(server, max_retries=1).history()
        calls = server.requests_for("get_history")

    assert len(entries) == 1001
    assert len(calls) == 3
    assert {call["body"]["length"] for call in calls} == {"1000"}
    assert [call["body"]["start"] for call in calls] == ["0", "0", "1000"]
