"""#144: request-path Plex writes and the connection tests fail fast.

An install runs one gunicorn worker with four threads, so an outbound call on
a request path must never wait long — the same failure mode as the poster
fetch of #139. S1: a Mark or an Undo makes one bounded attempt per Plex
collection call and keeps its durable request when the write fails. S2: the
Connections test, and ``/setup?test=1``, probe every service at once, with no
retries and a short timeout, so the whole answer is bounded even when every
service hangs. S3: the scan, which builds its own client, keeps its retries.

Every case goes through HTTP with the signed-in ``client`` fixture and a real
fake server; the outbound calls are read off ``requests`` itself, never
inferred from the app's own constants.
"""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from lifecycle_helpers import seed_item, seed_library

from almali_library_curator.config import save_settings
from almali_library_curator.web import views

TOKEN = "fail-fast-plex-token-4b71"
# S1 and S2: the most one outbound call may wait.
BOUNDED_CALL_SECONDS = 5.0
# The collection the fake Plex creates for the default collection name.
COLLECTION = "900001"


# -- helpers -----------------------------------------------------------------


def closed_port_url() -> str:
    """An address nothing listens on: a connection refused, at once."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


class HangingPlex:
    """A Plex address that accepts the connection and never answers (S1).

    The handler waits on a stop event instead of sleeping, so stopping the
    server also ends every request thread it is holding.
    """

    def __init__(self, hang_seconds: float = 30.0) -> None:
        self.hang_seconds = hang_seconds
        self.paths: list[str] = []
        self._stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "HangingPlex":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                owner._dispatch(self)

            def do_POST(self) -> None:
                owner._dispatch(self)

            def do_PUT(self) -> None:
                owner._dispatch(self)

            def do_DELETE(self) -> None:
                owner._dispatch(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()
        return self

    def _dispatch(self, handler) -> None:
        self.paths.append(handler.path)
        self._stop.wait(self.hang_seconds)
        try:
            handler.send_response(504)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        except OSError:
            pass

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "HangingPlex":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


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
    """Point the stored settings at a server, with the token from the env (S20)."""
    monkeypatch.setenv("ALC_PLEX_TOKEN", TOKEN)
    settings = replace(app.config["SETTINGS"], dry_run=False,
                       plex_rate_limit_per_second=1000, **overrides)
    save_settings(settings)
    return settings


def flashes(client):
    """The messages the last action flashed, taken so the next one starts clean."""
    with client.session_transaction() as session:
        return session.pop("_flashes", [])


def requests_to(server, path):
    return server.requests_for(path)


def outbound_to(calls, path):
    return [call for call in calls if urlsplit(call["url"]).path == path]


def marked_request(conn):
    return conn.execute("SELECT * FROM removal_requests ORDER BY id").fetchall()


# -- S1: Mark and Undo never wait long on Plex -------------------------------


def test_a_mark_reads_the_collections_once_and_keeps_its_durable_request(
    app, client, conn, monkeypatch, outbound
):
    """One attempt on the collection read, so a hung Plex costs one call (S1)."""
    data = FakePlexDataset()
    data.fail_always("/library/sections/1/collections", 503)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with FakePlexServer(data) as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url)

        response = client.post(f"/items/{item}/mark", data={"unit": "movie"})

        assert response.status_code == 303
        # The same outcome as always for a collection failure: no false Marked.
        assert flashes(client) == [("error", "web.flash.action_failed")]
        assert len(requests_to(plex, "/library/sections/1/collections")) == 1
        calls = outbound_to(outbound, "/library/sections/1/collections")
        assert [call["method"] for call in calls] == ["GET"]
        assert calls[0]["timeout"] is not None
        assert calls[0]["timeout"] <= BOUNDED_CALL_SECONDS
        # The durable request stays, for retry_collection_additions to finish.
        request = marked_request(conn)[0]
        assert request["state"] == "marked" and request["collection_added"] == 0
        assert data.collection_items == {}


def test_a_mark_adds_to_the_collection_once_and_keeps_its_durable_request(
    app, client, conn, monkeypatch, outbound
):
    """One attempt per add, so a hung Plex costs one call per member (S1)."""
    data = FakePlexDataset()
    data.create_collection("1", "Leaving soon")
    data.fail_always(f"/library/collections/{COLLECTION}/items/11", 503)
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with FakePlexServer(data) as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url)

        response = client.post(f"/items/{item}/mark", data={"unit": "movie"})

        assert response.status_code == 303
        assert flashes(client) == [("error", "web.flash.action_failed")]
        assert len(requests_to(plex, f"/library/collections/{COLLECTION}/items/11")) == 1
        calls = outbound_to(outbound, f"/library/collections/{COLLECTION}/items/11")
        assert [call["method"] for call in calls] == ["PUT"]
        assert calls[0]["timeout"] is not None
        assert calls[0]["timeout"] <= BOUNDED_CALL_SECONDS
        # Nothing was added, nothing was recorded as added.
        assert data.collection_items[COLLECTION] == set()
        request = marked_request(conn)[0]
        assert request["state"] == "marked" and request["collection_added"] == 0


def test_an_undo_removes_from_the_collection_once_and_keeps_the_member_tracked(
    app, client, conn, monkeypatch, outbound
):
    """One attempt per removal; a failure leaves the member for the retry (S1)."""
    data = FakePlexDataset()
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with FakePlexServer(data) as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url)
        assert client.post(f"/items/{item}/mark", data={"unit": "movie"}).status_code == 303
        assert flashes(client) == [("success", "web.flash.marked")]
        assert data.collection_items[COLLECTION] == {"11"}
        request_id = marked_request(conn)[0]["id"]
        data.fail_always(f"/library/collections/{COLLECTION}/items/11", 503)
        outbound.clear()

        response = client.post(f"/requests/{request_id}/undo", data={})

        assert response.status_code == 303
        # The cancellation itself is durable, and is what the owner is told.
        assert flashes(client) == [("success", "web.flash.undone")]
        assert len(requests_to(plex, f"/library/collections/{COLLECTION}/items/11")) == 2
        calls = outbound_to(outbound, f"/library/collections/{COLLECTION}/items/11")
        assert [call["method"] for call in calls] == ["DELETE"]
        assert calls[0]["timeout"] is not None
        assert calls[0]["timeout"] <= BOUNDED_CALL_SECONDS
        request = marked_request(conn)[0]
        assert request["state"] == "cancelled"
        # The member is still tracked, so retry_collection_cleanup finishes it.
        assert request["collection_added"] == 1
        assert data.collection_items[COLLECTION] == {"11"}


def test_an_undo_whose_collection_read_fails_still_cancels_and_keeps_the_member(
    app, client, conn, monkeypatch, outbound
):
    """The other Plex failure point of an Undo: the next scan finishes it (S1)."""
    data = FakePlexDataset()
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with FakePlexServer(data) as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url)
        assert client.post(f"/items/{item}/mark", data={"unit": "movie"}).status_code == 303
        assert flashes(client) == [("success", "web.flash.marked")]
        request_id = marked_request(conn)[0]["id"]
        # The collection read of the cleanup is the call that fails now.
        data.fail_always("/library/sections/1/collections", 503)
        outbound.clear()

        response = client.post(f"/requests/{request_id}/undo", data={})

        assert response.status_code == 303
        assert flashes(client) == [("success", "web.flash.undone")]
        assert len(outbound) == 1
        assert outbound[0]["timeout"] is not None
        assert outbound[0]["timeout"] <= BOUNDED_CALL_SECONDS
        request = marked_request(conn)[0]
        assert request["state"] == "cancelled"
        # The member stays tracked and held, for retry_collection_cleanup.
        assert request["collection_added"] == 1
        assert data.collection_items[COLLECTION] == {"11"}


def test_a_hanging_plex_answers_a_mark_in_one_bounded_call(app, client, conn, monkeypatch):
    """A Plex that never answers costs the request one short timeout, not minutes."""
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with HangingPlex() as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url, plex_timeout_seconds=0.25)

        started = time.monotonic()
        response = client.post(f"/items/{item}/mark", data={"unit": "movie"})
        elapsed = time.monotonic() - started

        assert response.status_code == 303
        assert elapsed < 2.0, elapsed
        assert flashes(client) == [("error", "web.flash.action_failed")]
        request = marked_request(conn)[0]
        assert request["state"] == "marked" and request["collection_added"] == 0


def test_a_mark_never_queues_behind_another_collection_write(app, client, conn, monkeypatch):
    """The process-wide collection lock is bounded, so a click never waits (S1)."""
    from almali_library_curator.plex.client import _COLLECTION_LOCK

    data = FakePlexDataset()
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    with FakePlexServer(data) as plex:
        to(app, monkeypatch, plex_base_url=plex.base_url, plex_timeout_seconds=0.25)
        # Another Mark, or a scan, is inside the list-then-create sequence.
        assert _COLLECTION_LOCK.acquire(timeout=1)
        try:
            answer: dict = {}

            def mark() -> None:
                answer["response"] = client.post(f"/items/{item}/mark",
                                                 data={"unit": "movie"})

            worker = threading.Thread(target=mark, daemon=True)
            started = time.monotonic()
            worker.start()
            worker.join(3.0)
            elapsed = time.monotonic() - started
        finally:
            _COLLECTION_LOCK.release()
        worker.join(3.0)

        assert not worker.is_alive(), "the Mark waited behind the held collection lock"
        assert elapsed < 2.0, elapsed
        assert answer["response"].status_code == 303
        assert flashes(client) == [("error", "web.flash.action_failed")]
        request = marked_request(conn)[0]
        assert request["state"] == "marked" and request["collection_added"] == 0
        assert data.collection_items == {}


# -- S1 (#155): an Undo never walks the cleanup backlog --------------------


def test_an_undo_never_walks_the_cleanup_backlog(app, client, conn, monkeypatch, outbound):
    """A backlog of six costs one Undo its own bounded call, not seven (S1).

    Against a Plex that never answers, the seven pending cleanups used to be
    walked one by one -- seven calls and seven timeouts for one click. The
    Undo now performs its own request's cleanup only, and the scan's retry
    still finishes everything the backlog owes, later.
    """
    from datetime import datetime, timezone

    from almali_library_curator.lifecycle import LifecycleEngine
    from almali_library_curator.plex import PlexClient
    from almali_library_curator.scan.settings import load_runtime_settings

    backlog = ("11", "12", "13", "14", "15", "16")
    data = FakePlexDataset()
    library = seed_library(conn, "1", "Movies")
    items = {key: seed_item(conn, library, key, f"Title {key}") for key in (*backlog, "17")}
    with FakePlexServer(data) as plex, HangingPlex() as hung:
        to(app, monkeypatch, plex_base_url=plex.base_url, plex_timeout_seconds=0.25)
        # A failed removal leaves each cancelled request owing its cleanup.
        for key in backlog:
            assert client.post(f"/items/{items[key]}/mark", data={"unit": "movie"}).status_code == 303
            assert flashes(client) == [("success", "web.flash.marked")]
            data.fail_always(f"/library/collections/{COLLECTION}/items/{key}", 503)
            request_id = marked_request(conn)[-1]["id"]
            assert client.post(f"/requests/{request_id}/undo", data={}).status_code == 303
            assert flashes(client) == [("success", "web.flash.undone")]
        assert len(marked_request(conn)) == 6
        assert len([row for row in marked_request(conn) if row["collection_added"]]) == 6
        assert client.post(f"/items/{items['17']}/mark", data={"unit": "movie"}).status_code == 303
        assert flashes(client) == [("success", "web.flash.marked")]
        request_id = marked_request(conn)[-1]["id"]

        # Plex now hangs: the click is the only thing that must stay short.
        to(app, monkeypatch, plex_base_url=hung.base_url, plex_timeout_seconds=0.25)
        outbound.clear()
        started = time.monotonic()
        response = client.post(f"/requests/{request_id}/undo", data={})
        elapsed = time.monotonic() - started

        assert response.status_code == 303
        assert flashes(client) == [("success", "web.flash.undone")]
        # One bounded call for its own request, whatever the backlog holds.
        assert elapsed < 1.0, (elapsed, len(outbound))
        assert len(outbound) == 1, [call["url"] for call in outbound]
        assert outbound[0]["timeout"] is not None
        assert outbound[0]["timeout"] <= BOUNDED_CALL_SECONDS
        # The backlog is untouched and still owed; its members are held.
        pending = [row for row in marked_request(conn) if row["collection_added"]]
        assert len(pending) == 7
        assert data.collection_items[COLLECTION] == set(backlog) | {"17"}

        # A later scan sweeps the backlog, as it already did: nothing is lost.
        data.always_fail.clear()
        to(app, monkeypatch, plex_base_url=plex.base_url, plex_timeout_seconds=0.25)
        settings = load_runtime_settings(app.config["SETTINGS"].config_dir)
        engine = LifecycleEngine(conn, settings=settings, clock=lambda: datetime.now(timezone.utc),
                                 collections=PlexClient.from_settings(settings))
        assert engine.retry_collection_cleanup() == 7
        assert data.collection_items[COLLECTION] == set()
        assert [row for row in marked_request(conn) if row["collection_added"]] == []


# -- S2: the connection tests are bounded -----------------------------------

def service_environment(url: str) -> dict[str, dict[str, str]]:
    """The environment that configures each card, pointed at ``url``."""
    return {
        "plex": {"ALC_PLEX_BASE_URL": url, "ALC_PLEX_TOKEN": TOKEN},
        "sonarr": {"ALC_SONARR_BASE_URL": url, "ALC_SONARR_API_KEY": "sonarr-key"},
        "radarr": {"ALC_RADARR_BASE_URL": url, "ALC_RADARR_API_KEY": "radarr-key"},
        "tautulli": {"ALC_TAUTULLI_BASE_URL": url, "ALC_TAUTULLI_API_KEY": "tautulli-key"},
        "tmdb": {"ALC_TMDB_BASE_URL": url, "ALC_TMDB_API_TOKEN": "tmdb-token"},
    }


@pytest.mark.parametrize("service", sorted(views.CONNECTION_SERVICES[:-1]))
def test_a_connection_test_makes_one_bounded_attempt_per_service(
    service, app, conn, monkeypatch, outbound
):
    """No retries and a short timeout for every probe, so the test is bounded."""
    for name, value in service_environment(closed_port_url())[service].items():
        monkeypatch.setenv(name, value)
    from almali_library_curator.scan.settings import load_runtime_settings
    settings = replace(load_runtime_settings(app.config["SETTINGS"].config_dir),
                       media_root="")

    model = views.connections_view(conn, settings)

    row = next(row for row in model.services if row.name == service)
    assert row.status == "unreachable"
    assert len(outbound) == 1, [call["url"] for call in outbound]
    assert outbound[0]["timeout"] is not None
    assert outbound[0]["timeout"] <= BOUNDED_CALL_SECONDS


def probing_barrier(services):
    """Probes that only answer if every one of them runs at the same time."""
    barrier = threading.Barrier(len(services), timeout=3.0)
    answered: list[str] = []

    def probe():
        barrier.wait()
        answered.append("answered")
        return object()

    return {name: probe for name in services}, answered


@pytest.fixture()
def every_service_configured(monkeypatch, tmp_path):
    """All six cards configured, so every probe of a live test has to run."""
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("ALC_MEDIA_ROOT", str(media))
    for environment in service_environment(closed_port_url()).values():
        for name, value in environment.items():
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize("path", ["/connections?test=1", "/setup?step=plex&test=1"])
def test_the_whole_connection_test_answers_at_once(every_service_configured, app, client,
                                                   monkeypatch, path):
    """Both screens probe concurrently, so a hanging service costs one call."""
    probes, answered = probing_barrier(views.CONNECTION_SERVICES)
    monkeypatch.setattr(views, "_probes", lambda settings: probes)

    started = time.monotonic()
    response = client.get(path)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert len(answered) == len(views.CONNECTION_SERVICES), "the probes ran one by one"
    assert elapsed < 2.0, elapsed


# -- S3: background work keeps its retries ----------------------------------


def test_the_scan_step_still_retries_a_transient_plex_failure(app, conn, monkeypatch):
    """The pipeline builds its own client, and that one keeps its retries (S3)."""
    from datetime import datetime, timezone

    from almali_library_curator.scan.pipeline import default_steps

    data = FakePlexDataset()
    data.items["1"] = [movie("11", "Tenet")]
    data.fail_once("/library/sections/1/all", 503)
    with FakePlexServer(data) as plex:
        monkeypatch.setenv("ALC_PLEX_TOKEN", TOKEN)
        settings = replace(app.config["SETTINGS"], plex_base_url=plex.base_url,
                           plex_rate_limit_per_second=1000)

        steps = default_steps(conn, settings, lambda: datetime.now(timezone.utc), None)
        steps["sync"]()

        # Two attempts: the 503 was retried instead of failing the step.
        assert len(plex.requests_for("/library/sections/1/all")) == 2
        synced = conn.execute("SELECT title FROM media_items").fetchall()
        assert [row["title"] for row in synced] == ["Tenet"]


def test_a_connection_test_answers_when_every_service_hangs(app, conn, monkeypatch):
    """The whole test is bounded: five hanging services answer in ~five seconds."""
    from almali_library_curator.scan.settings import load_runtime_settings

    with HangingPlex() as plex:
        for environment in service_environment(plex.base_url).values():
            for name, value in environment.items():
                monkeypatch.setenv(name, value)
        settings = replace(load_runtime_settings(app.config["SETTINGS"].config_dir),
                           media_root="")

        started = time.monotonic()
        model = views.connections_view(conn, settings)
        elapsed = time.monotonic() - started

        probed = [row.status for row in model.services if row.name != "host"]
        assert probed == ["unreachable"] * 5
        # Five services, one bounded attempt each, probed at the same time.
        assert elapsed < 10.0, elapsed
        assert len(plex.paths) == 5
