"""A fake plex.tv.

A real HTTP server that speaks the plex.tv v2 API surface the sign-in flow
uses (S13): the PIN endpoints and the resources list. Like the fake media
server, it exists so the app's own HTTP client is exercised over the wire
instead of through mocks of the app's code.

The PIN code is not a credential (S13 rule 5); the auth token is, so the
server records every request so tests can prove the token never travels in
a URL, a body echo or a log line.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def server_resource(
    name: str,
    client_identifier: str,
    *,
    provides: str = "server",
    owned: bool = True,
    local: bool = True,
    uri: str = "http://192.168.1.20:32400",
) -> dict:
    """One entry of ``GET /api/v2/resources``."""
    return {
        "name": name,
        "clientIdentifier": client_identifier,
        "provides": provides,
        "owned": owned,
        "connections": [
            {
                "protocol": "http",
                "address": uri.split("//")[1].split(":")[0],
                "port": int(uri.rsplit(":", 1)[1]) if ":" in uri.rsplit(":", 1)[-1] else 32400,
                "uri": uri,
                "local": 1 if local else 0,
            }
        ],
    }


class FakePlexTvDataset:
    """Everything the fake plex.tv knows about."""

    def __init__(self) -> None:
        # The account token a completed sign-in yields.
        self.token: str = "plex-account-token-9c2f61aa"
        # The pin appears authorized after this many polls of its endpoint.
        self.authorize_after_polls: int = 1
        # When set, the pin expires after this many polls (410 Gone).
        self.expire_after_polls: int | None = None
        # When set, every pin poll answers this status instead.
        self.fail_pin_polls: int | None = None
        self.pins: dict[str, dict] = {}
        self.poll_counts: dict[str, int] = {}
        self.expired: set[str] = set()
        self.next_pin_id = 4321
        # Injected failures, by path prefix.
        self.refuse_create: int | None = None
        self.fail_always: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        # When set, GET /api/v2/resources with another X-Plex-Token gets 401.
        self.required_token: str | None = None
        self.resources: list[dict] = [
            server_resource("Tower", "server-machine-identifier-1",
                            uri="http://192.168.1.20:32400"),
            server_resource("Backup", "server-machine-identifier-2",
                            uri="http://192.168.1.21:32400"),
            # Not a server: never offered to the owner.
            server_resource("Phone", "client-identifier-3", provides="player"),
            # Shared, not owned: never offered to the owner.
            server_resource("Friends", "server-machine-identifier-4", owned=False),
        ]

    def create_pin(self) -> dict:
        pin_id = str(self.next_pin_id)
        self.next_pin_id += 1
        pin = {"id": pin_id, "code": f"PIN{pin_id}CODE", "authToken": None}
        self.pins[pin_id] = pin
        return pin

    def poll(self, pin_id: str) -> dict | None:
        if pin_id not in self.pins or pin_id in self.expired:
            return None
        self.poll_counts[pin_id] = self.poll_counts.get(pin_id, 0) + 1
        if self.expire_after_polls is not None and self.poll_counts[pin_id] >= self.expire_after_polls:
            self.expired.add(pin_id)
            return None
        pin = dict(self.pins[pin_id])
        if self.poll_counts[pin_id] >= self.authorize_after_polls:
            pin["authToken"] = self.token
        return pin

    def expire(self, pin_id: str) -> None:
        self.expired.add(pin_id)


class FakePlexTvServer:
    """A plex.tv fake bound to a random localhost port."""

    def __init__(self, dataset: FakePlexTvDataset | None = None) -> None:
        self.dataset = dataset or FakePlexTvDataset()
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakePlexTvServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def _read_body(self, method: str) -> dict:
                if method != "POST":
                    return {}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return {}
                try:
                    decoded = json.loads(raw)
                except ValueError:
                    return {"_raw": raw.decode("utf-8", "replace")}
                return decoded if isinstance(decoded, dict) else {"_raw": str(decoded)}

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                owner.requests.append(
                    {
                        "path": parsed.path,
                        "query": query,
                        "headers": dict(self.headers),
                        "method": method,
                        "body": self._read_body(method),
                    }
                )
                status = owner.dataset.fail_always.get(parsed.path)
                if status is not None:
                    self._respond(status, {"errors": {"injected": "failure"}})
                    return
                location = owner.dataset.redirects.get(parsed.path)
                if location is not None:
                    body = json.dumps({}).encode("utf-8")
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                handler = owner._route(parsed.path, method)
                if handler is None:
                    self._respond(404, {"errors": {"code": 1024, "message": "Not found"}})
                    return
                status, payload = handler(query)
                self._respond(status, payload)

            def _respond(self, status: int, payload: dict | list) -> None:
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

    def __enter__(self) -> "FakePlexTvServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def requests_for(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    @property
    def pin_creations(self) -> list[dict]:
        return self.requests_for("/api/v2/pins")

    def _route(self, path: str, method: str):
        if path == "/api/v2/pins" and method == "POST":
            if self.dataset.refuse_create is not None:
                return lambda query: (self.dataset.refuse_create,
                                      {"errors": {"code": 1029, "message": "Not authorized"}})
            return lambda query: (201, self.dataset.create_pin())
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "v2", "pins"] and len(parts) == 4 and method == "GET":
            pin_id = parts[3]

            def poll(query) -> tuple[int, dict]:
                if self.dataset.fail_pin_polls is not None:
                    return (self.dataset.fail_pin_polls, {"errors": {"injected": "failure"}})
                pin = self.dataset.poll(pin_id)
                if pin is None:
                    # plex.tv answers 410 Gone for an expired or unknown pin.
                    return (410, {"errors": {"code": 1024, "message": "Gone"}})
                return (200, pin)

            return poll
        if path == "/api/v2/resources" and method == "GET":
            def resources(query) -> tuple[int, list]:
                required = self.dataset.required_token
                if required is not None:
                    seen = owner_token(self.requests[-1])
                    if seen != required:
                        return (401, {"errors": {"code": 1029, "message": "Unauthorized"}})
                return (200, self.dataset.resources)

            return resources
        return None


def owner_token(request: dict) -> str | None:
    return request["headers"].get("X-Plex-Token")
