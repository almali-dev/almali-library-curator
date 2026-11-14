"""A fake TMDB v3/v4 server.

A real HTTP server that speaks TMDB JSON, so the client is exercised over
the wire instead of through mocks of this app's own code. It authenticates
exactly like the real API: a v4 access token in the Authorization header.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def tmdb_movie(tmdb_id: int, title: str, **extra) -> dict:
    """One /3/movie/{id} answer, as TMDB reports it."""
    data = {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "vote_average": 7.3,
        "vote_count": 5200,
        "status": "Released",
        "release_date": "2020-08-26",
    }
    data.update(extra)
    return data


def tmdb_show(tmdb_id: int, name: str, **extra) -> dict:
    """One /3/tv/{id} answer, as TMDB reports it."""
    data = {
        "id": tmdb_id,
        "name": name,
        "vote_average": 8.1,
        "vote_count": 900,
        "status": "Returning Series",
        "first_air_date": "2018-04-13",
        "last_air_date": "2021-12-01",
        "number_of_seasons": 3,
    }
    data.update(extra)
    return data


class FakeTmdbDataset:
    """Everything the fake server knows about."""

    def __init__(self, access_token: str = "test-access-token") -> None:
        self.access_token = access_token
        self.movies: dict[int, dict] = {}
        self.shows: dict[int, dict] = {}
        self.failures: dict[str, list[int]] = {}
        self.always_fail: dict[str, int] = {}
        self.redirects: dict[str, str] = {}

    def fail_once(self, path: str, status: int = 503) -> None:
        self.failures.setdefault(path, []).append(status)

    def fail_always(self, path: str, status: int = 503) -> None:
        self.always_fail[path] = status


class FakeTmdbServer:
    """A TMDB fake bound to a random localhost port."""

    def __init__(self, dataset: FakeTmdbDataset | None = None) -> None:
        self.dataset = dataset or FakeTmdbDataset()
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeTmdbServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = parsed.query
                owner.requests.append(
                    {"path": parsed.path, "query": query, "headers": dict(self.headers)}
                )
                status = owner._failure_status(parsed.path)
                if status is not None:
                    self._respond(status, "injected failure")
                    return
                location = owner.dataset.redirects.get(parsed.path)
                if location is not None:
                    self._redirect(location)
                    return
                expected = f"Bearer {owner.dataset.access_token}"
                if owner.dataset.access_token and self.headers.get("Authorization") != expected:
                    self._respond(401, "Denied: you must be granted a valid key.")
                    return
                payload = owner._payload(parsed.path)
                if payload is None:
                    self._respond(404, "The resource you requested could not be found.")
                    return
                self._respond(200, payload)

            def _redirect(self, location: str) -> None:
                body = b"{}"
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _respond(self, status: int, payload: dict | str) -> None:
                if isinstance(payload, str):
                    payload = {"success": False, "status_code": 7, "status_message": payload}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeTmdbServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def requests_for(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def _failure_status(self, path: str) -> int | None:
        queued = self.dataset.failures.get(path)
        if queued:
            return queued.pop(0)
        return self.dataset.always_fail.get(path)

    def _payload(self, path: str) -> dict | None:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "3":
            kind, raw_id = parts[1], parts[2]
            if kind == "movie":
                return self.dataset.movies.get(int(raw_id))
            if kind == "tv":
                return self.dataset.shows.get(int(raw_id))
        return None
