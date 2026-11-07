"""A fake Radarr server.

A real HTTP server that speaks Radarr v3 JSON, so the client is exercised
over the wire instead of through mocks of this app's own code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def movie_resource(movie_id: int, title: str, **extra) -> dict:
    """A Radarr movie resource with one file."""
    data = {
        "id": movie_id,
        "title": title,
        "tmdbId": 2000 + movie_id,
        "monitored": True,
        "year": 2020,
        "path": f"/movies/{title}",
        "qualityProfileId": 1,
        "titleSlug": title.lower().replace(" ", "-"),
        "isAvailable": True,
        "hasFile": True,
        "sizeOnDisk": 24 * 1024**3,
        "added": "2024-04-01T00:00:00Z",
    }
    data.update(extra)
    return data


class FakeRadarrDataset:
    """Everything the fake server knows about."""

    def __init__(self) -> None:
        self.api_key: str = "test-api-key"
        self.movies: list[dict] = []
        self.import_exclusions: list[dict] = []
        self.on_delete_movie = lambda movie_id: None
        self.failures: dict[str, list[int]] = {}
        self.always_fail: dict[str, int] = {}
        self.redirects: dict[str, str] = {}

    def fail_once(self, request: str, status: int = 503) -> None:
        self.failures.setdefault(request, []).append(status)

    def fail_always(self, request: str, status: int = 503) -> None:
        self.always_fail[request] = status


class FakeRadarrServer:
    """A Radarr fake bound to a random localhost port."""

    def __init__(self, dataset: FakeRadarrDataset | None = None) -> None:
        self.dataset = dataset or FakeRadarrDataset()
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeRadarrServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                self._handle("GET")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                body = self._read_body()
                owner.requests.append(
                    {
                        "method": method,
                        "path": parsed.path,
                        "query": query,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                if owner.dataset.api_key and self.headers.get("X-Api-Key") != owner.dataset.api_key:
                    self._respond(401, {"message": "Unauthorized"})
                    return
                key = f"{method} {parsed.path}"
                status = owner._failure_status(key)
                if status is not None:
                    self._respond(status, {"message": "injected failure"})
                    return
                location = owner.dataset.redirects.get(key)
                if location is not None:
                    self._redirect(location)
                    return
                payload = owner._payload(method, parsed.path, query, body)
                if payload is None:
                    self._respond(404, {"message": "not found"})
                    return
                self._respond(200, payload)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}

            def _redirect(self, location: str) -> None:
                body = b"{}"
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _respond(self, status: int, payload) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
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

    def __enter__(self) -> "FakeRadarrServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def requests_for(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def _failure_status(self, request: str) -> int | None:
        queued = self.dataset.failures.get(request)
        if queued:
            return queued.pop(0)
        return self.dataset.always_fail.get(request)

    def _movie_by_id(self, movie_id: int) -> dict | None:
        for entry in self.dataset.movies:
            if entry.get("id") == movie_id:
                return entry
        return None

    def _payload(self, method: str, path: str, query: dict, body: dict):
        parts = path.strip("/").split("/")
        if parts == ["api", "v3", "system", "status"] and method == "GET":
            return {
                "appName": "Radarr",
                "version": "5.30.0.9730",
                "authentication": "external",
            }
        if parts == ["api", "v3", "movie"] and method == "GET":
            return self.dataset.movies
        if parts[:3] == ["api", "v3", "movie"] and len(parts) == 4:
            movie_id = int(parts[3])
            if method == "GET":
                return self._movie_by_id(movie_id)
            if method == "PUT":
                current = self._movie_by_id(movie_id)
                if current is None:
                    return None
                current.update(body)
                return current
            if method == "DELETE":
                if self._movie_by_id(movie_id) is None:
                    return None
                self.dataset.on_delete_movie(movie_id)
                self.dataset.movies = [
                    entry
                    for entry in self.dataset.movies
                    if entry.get("id") != movie_id
                ]
                return {}
            return None
        if parts == ["api", "v3", "importlistexclusion"] and method == "POST":
            created = {
                "id": len(self.dataset.import_exclusions) + 1,
                "tmdbId": body.get("tmdbId"),
                "title": body.get("title", ""),
            }
            self.dataset.import_exclusions.append(created)
            return created
        return None
