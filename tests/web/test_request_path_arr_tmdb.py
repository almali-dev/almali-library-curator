"""#155 S2/S3: the request-path arr revert and TMDB calls fail fast.

A reversal of the #144 scope. An Undo's Sonarr/Radarr revert and the TMDB
calls of a Mark's fresh-detection pass run on a request thread of the one
gunicorn worker (four threads), so each outbound call is one attempt with a
timeout of at most five seconds, built in the web layer exactly like the Plex
collection client of #144. A failure keeps today's outcome and messages: the
durable state stays, and the retries the scan already runs catch up. The
clients the scans and the deletion executor build from the settings keep their
retry ladder (S3).

Every case goes through HTTP with the signed-in ``client`` fixture and a real
fake server; the outbound calls are read off ``requests`` itself, never
inferred from the app's own constants.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest
import requests
from urllib.parse import urlsplit

from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from fake_tmdb_server import FakeTmdbDataset, FakeTmdbServer, tmdb_movie
from lifecycle_helpers import default_settings, seed_item, seed_library

from almali_library_curator.config import save_settings

TOKEN = "fail-fast-arr-tmdb-token-7c12"
# S2: the most one outbound call of a request path may wait.
BOUNDED_CALL_SECONDS = 5.0


# -- helpers -----------------------------------------------------------------


@pytest.fixture()
def outbound(monkeypatch):
    """Every outbound HTTP call the app makes, with the timeout it asked for."""
    calls: list[dict] = []
    real_request = requests.Session.request

    def recording(session, method, url, **kwargs):
        calls.append({"method": method, "url": url, "timeout": kwargs.get("timeout")})
        return real_request(session, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", recording)
    return calls


def to(app, monkeypatch, **overrides):
    """Point the stored settings at the live lifecycle, with the token in the env."""
    monkeypatch.setenv("ALC_PLEX_TOKEN", TOKEN)
    settings = replace(app.config["SETTINGS"], dry_run=False, **overrides)
    save_settings(settings)
    return settings


def flashes(client):
    """The messages the last action flashed, taken so the next one starts clean."""
    with client.session_transaction() as session:
        return session.pop("_flashes", [])


def marked(client, item):
    """Mark one seeded item through the public form; return its request id."""
    assert client.post(f"/items/{item}/mark", data={"unit": "movie"}).status_code == 303
    assert flashes(client) == [("success", "web.flash.marked")]


def request_row(conn, request_id):
    return conn.execute(
        "SELECT * FROM removal_requests WHERE id = ?", (request_id,)
    ).fetchone()


def last_request_id(conn) -> int:
    return int(conn.execute("SELECT max(id) FROM removal_requests").fetchone()[0])


def outbound_to(calls, path):
    return [call for call in calls if urlsplit(call["url"]).path == path]


def leave_effects_applied(conn, request_id, effects) -> None:
    """The durable state a removal that failed after the arr writes leaves."""
    conn.execute(
        "UPDATE removal_requests SET state = 'removal_failed', applied_effects = ?"
        " WHERE id = ?",
        (json.dumps(effects), request_id),
    )
    conn.commit()


@contextmanager
def fake_arr(service, fetch_path):
    """A fake arr that answers 503 on the read a revert starts with."""
    if service == "radarr":
        data = FakeRadarrDataset()
        data.movies.append(movie_resource(1, "Tenet"))
        data.fail_always(fetch_path, 503)
        with FakeRadarrServer(data) as server:
            yield data, {"radarr_base_url": server.base_url, "radarr_api_key": data.api_key}
    else:
        data = FakeSonarrDataset()
        data.series.append(series_resource(1, "Show"))
        data.fail_always(fetch_path, 503)
        with FakeSonarrServer(data) as server:
            yield data, {"sonarr_base_url": server.base_url, "sonarr_api_key": data.api_key}


# -- S2: the Undo's arr revert is one bounded attempt ------------------------

REVERTS = {
    "radarr": ("/api/v3/movie/1", [{"kind": "unmonitor", "arr": "radarr", "resource_id": 1}]),
    "sonarr": (
        "/api/v3/series/1",
        [{"kind": "unmonitor", "arr": "sonarr", "resource_id": 1, "seasons": [1]}],
    ),
}


@pytest.mark.parametrize("service", sorted(REVERTS))
def test_the_undo_revert_makes_one_bounded_attempt(
    service, app, client, conn, monkeypatch, outbound
):
    """One attempt, at most five seconds, and today's outcome on failure (S2)."""
    fetch_path, effects = REVERTS[service]
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    to(app, monkeypatch)
    marked(client, item)
    request_id = last_request_id(conn)
    leave_effects_applied(conn, request_id, effects)

    with fake_arr(service, fetch_path) as (data, configured):
        to(app, monkeypatch, **configured)
        outbound.clear()

        response = client.post(f"/requests/{request_id}/undo", data={})

        assert response.status_code == 303
        # The revert failed, so the cancellation refused: today's message.
        assert flashes(client) == [("error", "web.flash.action_failed")]
        calls = outbound_to(outbound, fetch_path)
        assert len(calls) == 1, [call["url"] for call in outbound]
        assert calls[0]["timeout"] is not None
        assert calls[0]["timeout"] <= BOUNDED_CALL_SECONDS
        # Nothing was reverted, and the record still names what is applied.
        stored = request_row(conn, request_id)
        assert stored["state"] == "removal_failed"
        assert json.loads(stored["applied_effects"]) == effects


def test_a_failed_revert_of_a_hung_arr_answers_in_one_bounded_call(
    app, client, conn, monkeypatch, outbound
):
    """A hanging Radarr costs the click one call, never the retry ladder (S2)."""
    data = FakeRadarrDataset()
    data.movies.append(movie_resource(1, "Tenet"))
    data.fail_always("/api/v3/movie/1", 503)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    to(app, monkeypatch)
    marked(client, item)
    request_id = last_request_id(conn)
    leave_effects_applied(conn, request_id, REVERTS["radarr"][1])
    with FakeRadarrServer(data) as radarr:
        to(app, monkeypatch, radarr_base_url=radarr.base_url, radarr_api_key=data.api_key)
        outbound.clear()

        response = client.post(f"/requests/{request_id}/undo", data={})

        assert response.status_code == 303
        assert flashes(client) == [("error", "web.flash.action_failed")]
        # Nothing else was asked of any service, at any retry.
        assert len(outbound) == 1, [call["url"] for call in outbound]
        assert outbound[0]["timeout"] is not None
        assert outbound[0]["timeout"] <= BOUNDED_CALL_SECONDS


# -- S2: the Mark's fresh detection asks TMDB once ---------------------------


def test_the_marks_fresh_detection_makes_one_bounded_tmdb_attempt(
    app, client, conn, monkeypatch, outbound
):
    """One TMDB attempt on the request path, and the Mark is unchanged (S2)."""
    data = FakeTmdbDataset()
    data.movies[42] = tmdb_movie(42, "Tenet", vote_average=8.9)
    data.fail_always("/3/movie/42", 503)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    conn.execute("UPDATE media_items SET tmdb_id = 42 WHERE id = ?", (item,))
    conn.commit()
    with FakeTmdbServer(data) as tmdb:
        to(app, monkeypatch, tmdb_base_url=tmdb.base_url)
        monkeypatch.setenv("ALC_TMDB_API_TOKEN", data.access_token)
        outbound.clear()

        response = client.post(f"/items/{item}/mark", data={"unit": "movie"})

        assert response.status_code == 303
        # The failure changes nothing the owner sees, as today: still Marked.
        assert flashes(client) == [("success", "web.flash.marked")]
        calls = outbound_to(outbound, "/3/movie/42")
        assert len(calls) == 1, [call["url"] for call in outbound]
        assert calls[0]["timeout"] is not None
        assert calls[0]["timeout"] <= BOUNDED_CALL_SECONDS
        assert request_row(conn, last_request_id(conn))["state"] == "marked"


# -- S3: the scan keeps its retries -----------------------------------------


def test_the_scans_tmdb_client_still_retries_a_transient_failure(conn):
    """The scan builds its own client, and that one keeps its retries (S3)."""
    from datetime import datetime, timezone

    from almali_library_curator.scan.pipeline import build_detection

    data = FakeTmdbDataset()
    data.movies[42] = tmdb_movie(42, "Tenet")
    data.fail_once("/3/movie/42", 503)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    conn.execute("UPDATE media_items SET tmdb_id = 42 WHERE id = ?", (item,))
    conn.commit()
    with FakeTmdbServer(data) as tmdb:
        settings = default_settings(
            tmdb_base_url=tmdb.base_url, tmdb_api_token=data.access_token
        )

        detection = build_detection(conn, settings, lambda: datetime.now(timezone.utc))
        outcome = detection.scan_items([item])

        # Two attempts: the 503 was retried instead of failing the pass.
        assert len(tmdb.requests_for("/3/movie/42")) == 2
        assert [row.rating for row in outcome.candidates] == [7.3]
