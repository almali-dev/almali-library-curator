"""A fake Plex Media Server.

A real HTTP server that speaks Plex-like JSON, so the client is exercised
over the wire instead of through mocks of this app's own code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def movie(rating_key: str, title: str, **extra) -> dict:
    """A movie metadata entry whose files live in ``Media[].Part[]``.

    Mirrors the payload a live server answers for
    ``/library/sections/<key>/all``: no top-level ``Part``. Pass ``Media=``
    to build multi-version items, or ``Part=`` to build the older
    top-level shape the parser still accepts as a fallback.
    """
    if "Media" in extra or "Part" in extra:
        media = extra.pop("Media", [])
    else:
        media = [
            {
                "id": int(rating_key) if str(rating_key).isdigit() else 1,
                "videoProfile": "high",
                "Part": [
                    {
                        "container": "MKV",
                        "duration": 2_700_000,
                        "file": f"/media/Movies/{title}/{title}.mkv",
                        "id": 12345,
                        "indexes": "sd",
                        "key": f"/library/parts/{rating_key}/file.mkv",
                        "size": 24 * 1024**3,
                        "videoProfile": "high",
                    }
                ],
            }
        ]
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "movie",
        "title": title,
        "addedAt": 1711929600,
        "lastViewedAt": 1711929600,
        "rating": 7.3,
        "leafCount": 1,
        "viewedLeafCount": 1,
        "Media": media,
    }
    data.update(extra)
    return data


def series(rating_key: str, title: str, **extra) -> dict:
    """A series metadata entry without a file of its own."""
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "series",
        "title": title,
        "addedAt": 1711929600,
        "leafCount": 12,
        "viewedLeafCount": 6,
        "childCount": 2,
        "viewedChildCount": 1,
    }
    data.update(extra)
    return data


def season(rating_key: str, series_key: str, number: int, **extra) -> dict:
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "season",
        "title": f"Season {number}",
        "parentRatingKey": series_key,
        "index": number,
        "leafCount": 6,
        "viewedLeafCount": 0,
    }
    data.update(extra)
    return data


def episode(rating_key: str, series_key: str, season_key: str, number: int, **extra) -> dict:
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "episode",
        "title": f"Episode {number}",
        "grandparentRatingKey": series_key,
        "parentRatingKey": season_key,
        "index": number,
        "duration": 2_700_000,
        "Media": [
            {
                "id": int(rating_key) if str(rating_key).isdigit() else 1,
                "videoProfile": "high",
                "Part": [
                    {
                        "container": "MKV",
                        "duration": 2_700_000,
                        "file": f"/media/TV/{season_key}/{number}.mkv",
                        "id": 12345,
                        "indexes": "sd",
                        "key": f"/library/parts/{rating_key}/file.mkv",
                        "size": 2 * 1024**3,
                        "videoProfile": "high",
                    }
                ],
            }
        ],
    }
    data.update(extra)
    return data


def history_entry(
    rating_key: str,
    account_id: int,
    viewed_at: int,
    view_offset: int,
    duration: int,
    **extra,
) -> dict:
    data = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "movie",
        "title": "Watched item",
        "accountID": account_id,
        "viewedAt": viewed_at,
        "viewOffset": view_offset,
        "duration": duration,
    }
    data.update(extra)
    return data


TYPE_NAMES = {"1": "movie", "2": "series", "3": "season", "4": "episode"}


class FakePlexDataset:
    """Everything the fake server knows about."""

    def __init__(self) -> None:
        self.sections: list[dict] = [
            {"key": "1", "title": "Movies", "type": "movie"},
            {"key": "2", "title": "Shows", "type": "show"},
        ]
        self.items: dict[str, list[dict]] = {"1": [], "2": []}
        self.children: dict[str, list[dict]] = {}
        self.accounts: list[dict] = [{"id": 1, "name": "Marc"}, {"id": 2, "name": "Anna"}]
        self.history: list[dict] = []
        self.collections: dict[str, list[dict]] = {}
        # Collection membership, by collection rating key (write endpoints).
        self.collection_items: dict[str, set[str]] = {}
        self.next_collection_id: int = 900001
        self.failures: dict[str, list[int]] = {}
        self.always_fail: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        # The server identity answered at "/" (S12 test before save).
        self.identity: dict = {
            "machineIdentifier": "fake-plex-machine-identifier",
            "friendlyName": "Fake Plex",
            "version": "1.41.0",
        }
        # When set, every request with another X-Plex-Token gets a 401.
        self.required_token: str | None = None
        # When set, the section reads of the seasons and the episodes answer
        # nothing, so the sync has to fall back to the per-item reads.
        self.bulk_reads_answer_nothing = False
        # Explicit per-key metadata entries for the single-item read
        # (``GET /library/metadata/{key}``), used by the S3 deletion guard.
        self.metadata: dict[str, dict] = {}
        # DELETE /library/metadata/{key} (S15 rule 4). A real Plex answers
        # 200 with an empty body; ``on_delete_media`` stands for the server
        # removing the item's file, and a key in ``missing_media`` is a 404.
        self.on_delete_media = lambda rating_key: None
        self.missing_media: set[str] = set()
        self.deleted_media: list[str] = []

    def fail_once(self, path: str, status: int = 503) -> None:
        self.failures.setdefault(path, []).append(status)

    def fail_always(self, path: str, status: int = 503) -> None:
        self.always_fail[path] = status

    def create_collection(self, section_key: str, title: str) -> dict:
        """Store a new collection in a section and return its entry."""
        key = str(self.next_collection_id)
        self.next_collection_id += 1
        entry = {
            "ratingKey": key,
            "key": f"/library/collections/{key}",
            "type": "collection",
            "title": title,
            "childCount": 0,
        }
        self.collections.setdefault(section_key, []).append(entry)
        self.collection_items.setdefault(key, set())
        return entry

    def find_collection(self, rating_key: str) -> dict | None:
        for entries in self.collections.values():
            for entry in entries:
                if entry["ratingKey"] == rating_key:
                    return entry
        return None

    def find_metadata(self, rating_key: str) -> dict | None:
        """One item by rating key, for the single-item metadata read.

        The explicit ``metadata`` entries win, so a test can override the
        shape an item answers with (for example a multi-version item);
        otherwise the section items and the children are searched.
        """
        entry = self.metadata.get(rating_key)
        if entry is not None:
            return entry
        for items in self.items.values():
            for item in items:
                if str(item.get("ratingKey")) == rating_key:
                    return item
        for children in self.children.values():
            for child in children:
                if str(child.get("ratingKey")) == rating_key:
                    return child
        return None

    def section_children(self, section_key: str, child_type: str) -> list[dict]:
        """The seasons or the episodes of a section.

        A Plex section read answers the same children the per-item reads
        answer, so the fake server derives them from one source: what the
        section read returns and what the per-item reads return are the
        same rows.
        """
        children: list[dict] = []
        for item in self.items.get(section_key, []):
            if item.get("type") != "series":
                continue
            for season_entry in self.children.get(str(item["ratingKey"]), []):
                if season_entry.get("type") != "season":
                    continue
                if child_type == "season":
                    children.append(season_entry)
                else:
                    children.extend(
                        episode
                        for episode in self.children.get(str(season_entry["ratingKey"]), [])
                        if episode.get("type") == "episode"
                    )
        return children


class FakePlexServer:
    """A Plex Media Server fake bound to a random localhost port."""

    def __init__(self, dataset: FakePlexDataset | None = None) -> None:
        self.dataset = dataset or FakePlexDataset()
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "FakePlexServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                body: dict = {}
                if method in ("POST", "PUT"):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        try:
                            decoded = json.loads(raw)
                        except ValueError:
                            decoded = {}
                        body = decoded if isinstance(decoded, dict) else {}
                owner.requests.append(
                    {
                        "path": parsed.path,
                        "query": query,
                        "headers": dict(self.headers),
                        "method": method,
                        "body": body,
                    }
                )
                required = owner.dataset.required_token
                if required is not None and self.headers.get("X-Plex-Token") != required:
                    self._respond(401, {"error": "unauthorized"})
                    return
                status = owner._failure_status(parsed.path, query)
                if status is not None:
                    self._respond(status, {"error": "injected failure"})
                    return
                location = owner.dataset.redirects.get(parsed.path)
                if location is not None:
                    self._redirect(location)
                    return
                parts = parsed.path.strip("/").split("/")
                if method == "DELETE" and parts[:2] == ["library", "metadata"] and len(parts) == 3:
                    if parts[2] in owner.dataset.missing_media:
                        self._respond(404, {"error": "not found"})
                        return
                    owner.dataset.deleted_media.append(parts[2])
                    owner.dataset.on_delete_media(parts[2])
                    self._respond_empty(200)
                    return
                payload = owner._payload(parsed.path, query, method=method, body=body)
                if payload is None:
                    self._respond(404, {"error": "not found"})
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

            def _respond_empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _respond(self, status: int, payload: dict) -> None:
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

    def __enter__(self) -> "FakePlexServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def requests_for(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def _failure_status(self, path: str, query: dict) -> int | None:
        # A failure can be aimed at one type of a section read, so a server
        # can refuse the bulk read of the seasons while the rest of the API
        # keeps working.
        keys = [path]
        if query.get("type"):
            keys.insert(0, f"{path}?type={query['type']}")
        for key in keys:
            queued = self.dataset.failures.get(key)
            if queued:
                return queued.pop(0)
            if key in self.dataset.always_fail:
                return self.dataset.always_fail[key]
        return None

    def _paged(self, items: list[dict], query: dict) -> dict:
        start = int(query.get("X-Plex-Container-Start", 0))
        size = int(query.get("X-Plex-Container-Size", len(items) or 1))
        page = items[start : start + size]
        return {
            "MediaContainer": {
                "size": len(page),
                "totalSize": len(items),
                "offset": start,
                "Metadata": page,
            }
        }

    def _payload(
        self, path: str, query: dict, method: str = "GET", body: dict | None = None
    ) -> dict | None:
        if path == "/":
            return {"MediaContainer": dict(self.dataset.identity)}
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "size": len(self.dataset.sections),
                    "Directory": self.dataset.sections,
                }
            }
        if path == "/accounts":
            return {
                "MediaContainer": {
                    "size": len(self.dataset.accounts),
                    "Account": self.dataset.accounts,
                }
            }
        if path == "/status/sessions/history/all":
            return self._paged(self.dataset.history, query)
        parts = path.strip("/").split("/")
        if parts[:2] == ["library", "sections"] and len(parts) == 4:
            section_key, leaf = parts[2], parts[3]
            if leaf == "all":
                wanted = TYPE_NAMES.get(query.get("type", ""), query.get("type"))
                if wanted in ("season", "episode"):
                    items = (
                        []
                        if self.dataset.bulk_reads_answer_nothing
                        else self.dataset.section_children(section_key, wanted)
                    )
                else:
                    items = self.dataset.items.get(section_key, [])
                    if wanted:
                        items = [item for item in items if item.get("type") == wanted]
                return self._paged(items, query)
            if leaf == "collections":
                if method == "POST":
                    title = str(
                        (body or {}).get("title") or query.get("title") or ""
                    )
                    entry = self.dataset.create_collection(section_key, title)
                    return {
                        "MediaContainer": {
                            "size": 1,
                            "librarySectionID": section_key,
                            "Metadata": [entry],
                        }
                    }
                return self._paged(self.dataset.collections.get(section_key, []), query)
        if parts[:2] == ["library", "metadata"] and parts[3:] == ["children"]:
            return self._paged(self.dataset.children.get(parts[2], []), query)
        if parts[:2] == ["library", "metadata"] and len(parts) == 3:
            entry = self.dataset.find_metadata(parts[2])
            if entry is None:
                return None
            return {"MediaContainer": {"size": 1, "Metadata": [entry]}}
        if (
            parts[:2] == ["library", "collections"]
            and len(parts) == 5
            and parts[3] == "items"
            and method in ("PUT", "DELETE")
        ):
            collection_key, item_key = parts[2], parts[4]
            if self.dataset.find_collection(collection_key) is None:
                return None
            members = self.dataset.collection_items.setdefault(collection_key, set())
            if method == "PUT":
                members.add(item_key)
            else:
                members.discard(item_key)
            return {"MediaContainer": {"size": len(members)}}
        return None
