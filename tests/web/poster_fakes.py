"""A Plex fake that answers image bytes instead of JSON (#139).

``tests/plex/fake_plex_server.py`` speaks Plex JSON, which is what the sync
reads; the poster route needs the other half of the server: a binary body
with a content type, a redirect, a body over the cap and an unreachable host.
This module adds that small server and records every request with its
headers, which is how the tests prove the token travels as ``X-Plex-Token``
and never in the URL.

The client-level test in ``tests/plex/test_plex_client_images.py`` imports
this module too, so it deliberately depends on nothing but the standard
library.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# The shape of a stored Plex poster path (S3), and the size the route asks the
# photo transcoder for (S4).
THUMB = "/library/metadata/11/thumb/1700000000"
OTHER_THUMB = "/library/metadata/12/thumb/1700000100"
TRANSCODER = "/photo/:/transcode"

# Small bodies with the signature each accepted type starts with, so a test
# can tell one image from another without a real encoder.
PNG = b"\x89PNG\r\n\x1a\n" + b"poster-bytes" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"poster-bytes" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"poster-bytes" * 16
# One byte over the 5 MiB the client accepts (S5).
OVER_CAP = b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)


class FakePosterPlexServer:
    """A Plex Media Server fake that serves images on a localhost port.

    ``images`` maps a path to the ``(content type, body)`` it answers;
    ``statuses`` maps a path to an error status, ``redirects`` to a
    ``Location``, and ``fail_all`` makes every request fail. Anything else is
    a 404, like a path a real server does not have.
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.requests: list[dict] = []
        self.images: dict[str, tuple[str, bytes]] = {}
        self.statuses: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        self.fail_all: int | None = None
        # A body without ``Content-Length`` is read until the connection
        # closes, which is how a streamed body reaches the client.
        self.no_content_length = False
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakePosterPlexServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                try:
                    self._handle()
                except (BrokenPipeError, ConnectionResetError):
                    # A client that refuses a body over the cap hangs up
                    # while the answer is still being written.
                    pass

            def _handle(self) -> None:
                parsed = urlparse(self.path)
                owner.requests.append(
                    {
                        "method": "GET",
                        "path": parsed.path,
                        "raw_path": self.path,
                        "query": {key: values[0] for key, values in parse_qs(parsed.query).items()},
                        "headers": dict(self.headers),
                    }
                )
                if owner.token is not None and self.headers.get("X-Plex-Token") != owner.token:
                    self._send(401, "text/plain", b"unauthorized")
                    return
                if owner.fail_all is not None:
                    self._send(owner.fail_all, "text/plain", b"plex is failing")
                    return
                location = owner.redirects.get(parsed.path)
                if location is not None:
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = owner.statuses.get(parsed.path)
                if status is not None:
                    self._send(status, "text/plain", b"plex answered an error")
                    return
                image = owner.images.get(parsed.path)
                if image is None and parsed.path == TRANSCODER:
                    # The real transcoder serves the artwork of the ``url`` it
                    # was asked for, so a test needs one entry per thumb.
                    image = owner.images.get(
                        {key: values[0] for key, values in parse_qs(parsed.query).items()}
                        .get("url", "")
                    )
                if image is None:
                    self._send(404, "text/plain", b"no such path")
                    return
                self._send(200, image[0], image[1])

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if not owner.no_content_length:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
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

    def __enter__(self) -> "FakePosterPlexServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]

    def requests_for(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def serve(self, path: str = THUMB, content_type: str = "image/png",
              body: bytes = PNG) -> None:
        """Answer ``path`` with an image (and the transcoder for that thumb)."""
        self.images[path] = (content_type, body)
