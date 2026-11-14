"""A fake Tautulli v2 server.

A real HTTP server that speaks Tautulli v2 JSON, so the client is exercised
over the wire instead of through mocks of this app's own code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def history_row(row_id: int, rating_key: int, user_id: int, user: str, **extra) -> dict:
    """One get_history data row, as Tautulli reports it."""
    data = {
        "id": row_id,
        "date": 1711929600,
        "started": 1711929600,
        "stopped": 1711932300,
        "paused_counter": 0,
        "rating_key": rating_key,
        "user_id": user_id,
        "user": user,
        "friendly_name": user,
        "platform": "Roku",
        "player": "Living Room",
        "full_title": f"Show - S01E{row_id:02d} - Episode {row_id}",
        "title": f"Episode {row_id}",
        "grandparent_title": "Show",
        "parent_media_index": 1,
        "media_index": row_id,
        "media_type": "episode",
        "view_offset": 2_700_000,
        "duration": 2_700_000,
        "percent_complete": 100,
    }
    data.update(extra)
    return data


def tautulli_user(user_id: int, username: str, **extra) -> dict:
    """One get_users entry, as Tautulli reports it."""
    data = {
        "user_id": user_id,
        "username": username,
        "friendly_name": username,
        "email": f"{username.lower()}@example.com",
        "is_admin": 1 if user_id == 1 else 0,
        "is_home_user": 1,
        "is_allow_sync": 1,
        "is_restricted": 0,
        "do_notify": 1,
        "keep_history": 1,
        "allow_guest": 0,
        "deleted_user": 0,
    }
    data.update(extra)
    return data


class FakeTautulliDataset:
    """Everything the fake server knows about."""

    def __init__(self, api_key: str = "test-api-key") -> None:
        self.api_key = api_key
        self.users: list[dict] = []
        self.history: list[dict] = []
        self.failures: dict[str, list[int]] = {}
        self.always_fail: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        self.command_errors: dict[str, str] = {}

    def fail_once(self, cmd: str, status: int = 503) -> None:
        self.failures.setdefault(cmd, []).append(status)

    def fail_always(self, cmd: str, status: int = 503) -> None:
        self.always_fail[cmd] = status

    def fail_command(self, cmd: str, message: str) -> None:
        """Tautulli answers HTTP 200 with result "error" for bad parameters."""
        self.command_errors[cmd] = message


class FakeTautulliServer:
    """A Tautulli v2 fake bound to a random localhost port."""

    def __init__(self, dataset: FakeTautulliDataset | None = None) -> None:
        self.dataset = dataset or FakeTautulliDataset()
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeTautulliServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                self._handle("GET", {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                body = {key: value[0] for key, value in parse_qs(raw).items()}
                self._handle("POST", body)

            def _handle(self, method: str, body: dict) -> None:
                parsed = urlparse(self.path)
                query = {
                    key: value[0] for key, value in parse_qs(parsed.query).items()
                }
                cmd = body.get("cmd") or query.get("cmd")
                owner.requests.append(
                    {
                        "method": method,
                        "path": parsed.path,
                        "cmd": cmd,
                        "query": query,
                        "body": body,
                        "headers": dict(self.headers),
                    }
                )
                status = owner._failure_status(cmd)
                if status is not None:
                    self._respond(status, {"response": {"result": "error", "message": "injected failure"}})
                    return
                location = owner.dataset.redirects.get(cmd)
                if location is not None:
                    self._redirect(location)
                    return
                command_error = owner.dataset.command_errors.get(cmd)
                if command_error is not None:
                    self._respond(200, {"response": {"result": "error", "message": command_error}})
                    return
                if owner.dataset.api_key and body.get("apikey") != owner.dataset.api_key:
                    self._respond(401, {"response": {"result": "error", "message": "Unauthorized"}})
                    return
                payload = owner._payload(cmd, body)
                if payload is None:
                    self._respond(404, {"response": {"result": "error", "message": "not found"}})
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

            def _respond(self, status: int, payload: dict) -> None:
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

    def __enter__(self) -> "FakeTautulliServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def requests_for(self, cmd: str) -> list[dict]:
        return [request for request in self.requests if request["cmd"] == cmd]

    def _failure_status(self, cmd: str | None) -> int | None:
        queued = self.dataset.failures.get(cmd)
        if queued:
            return queued.pop(0)
        return self.dataset.always_fail.get(cmd)

    def _paged_history(self, query: dict) -> dict:
        start = int(query.get("start", 0))
        length = int(query.get("length", 25))
        page = self.dataset.history[start : start + length]
        total = len(self.dataset.history)
        return {
            "draw": 0,
            "recordsTotal": total,
            "recordsFiltered": total,
            "data": page,
        }

    def _payload(self, cmd: str | None, query: dict) -> dict | None:
        if cmd == "get_history":
            return {"response": {"result": "success", "data": self._paged_history(query)}}
        if cmd == "get_users":
            return {"response": {"result": "success", "data": self.dataset.users}}
        return None
