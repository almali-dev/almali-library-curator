"""The poster of one item, served by the app (#139).

S2: the browser learns neither the Plex address nor the token; the image is
one response of this app, behind the owner login. S3: only a thumb the sync
stored, and only when it has the shape Plex sends, ever becomes a request, and
that request goes through :class:`PlexClient` (throttle, retries, redirect
refusal and the token header). S4: each image is kept on disk under the config
directory, named by the digest of the thumb and the requested size, and served
for a day from the browser's own private cache. S5: every failure is a
placeholder that nothing caches, so the next request asks Plex again.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import tempfile

from flask import Response, abort, current_app

from almali_library_curator.plex import PlexClient
from almali_library_curator.plex.client import PlexError
from almali_library_curator.plex.posters import is_poster_thumb
from almali_library_curator.web.resources import get_db, get_settings

logger = logging.getLogger(__name__)

# S5: the small poster the photo transcoder builds from the stored thumb.
POSTER_WIDTH = 240
POSTER_HEIGHT = 360
TRANSCODER_PATH = "/photo/:/transcode"

# S4: only these three types are an image this route will serve, and the
# suffix each one keeps in the cache.
ACCEPTED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
IMAGE_CACHE_CONTROL = "private, max-age=86400"

# S4: one directory under the config directory, one file per thumb and size.
POSTER_DIRECTORY = "posters"
# S5: the neutral image a failure gets instead of a broken one.
PLACEHOLDER_ASSET = "poster-placeholder.svg"


def poster_response(media_item_id: int) -> Response:
    """The poster of one item, or the placeholder, or a 404 (S3, S4, S5)."""
    thumb = stored_thumb(get_db(), media_item_id)
    if not is_poster_thumb(thumb) or not isinstance(thumb, str):
        # S3 and S5: an unknown item, no thumb, or a stored value that is not
        # the shape Plex sends is a 404, and Plex is never asked anything.
        abort(404)
    settings = get_settings()
    key = cache_key(thumb)
    directory = _directory(settings.config_dir)
    cached = _read_cache(directory, key)
    if cached is not None:
        return _image_response(*cached)
    image = _fetch(media_item_id, thumb, settings)
    if image is None:
        return _placeholder()
    _write_cache(directory, key, *image)
    return _image_response(*image)


def stored_thumb(conn, media_item_id: int) -> str | None:
    """S3: the thumb the sync stored for the item, or for a dry run's row.

    A dry run never writes the live ``media_items``, so its thumb travels in
    the published snapshot; the item's own column wins when both carry one.
    """
    row = conn.execute(
        "SELECT thumb FROM media_items WHERE id = ?", (media_item_id,)
    ).fetchone()
    if row is not None and row["thumb"]:
        return row["thumb"]
    row = conn.execute(
        "SELECT thumb FROM scan_rows WHERE media_item_id = ? AND thumb IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (media_item_id,),
    ).fetchone()
    return row["thumb"] if row is not None else None


def cache_key(thumb: str) -> str:
    """S4: the name of one image's file, from the thumb and the size asked for.

    Plex's thumb path carries a timestamp, so new artwork is a new key.
    """
    material = f"{thumb}|{POSTER_WIDTH}x{POSTER_HEIGHT}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _directory(config_dir) -> Path:
    return Path(config_dir) / POSTER_DIRECTORY


def _read_cache(directory: Path, key: str) -> tuple[bytes, str] | None:
    """The cached image and its type, or ``None`` when the disk has none."""
    for content_type, suffix in ACCEPTED_TYPES.items():
        path = directory / f"{key}{suffix}"
        try:
            return path.read_bytes(), content_type
        except FileNotFoundError:
            continue
        except OSError:
            # An unreadable cache file is a miss, never a failed poster.
            logger.warning("poster_cache_unreadable")
            return None
    return None


def _write_cache(directory: Path, key: str, body: bytes, content_type: str) -> None:
    """S4: one image written whole, then renamed into its place."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".poster.")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(body)
            os.replace(temporary, directory / f"{key}{ACCEPTED_TYPES[content_type]}")
        except BaseException:
            os.unlink(temporary)
            raise
    except OSError:
        # A cache that cannot be written is not a failed poster: the image is
        # still served, and the next request tries the disk again.
        logger.warning("poster_cache_unwritable")


def _attempts(thumb: str) -> tuple[tuple[str, dict[str, str] | None], ...]:
    """S5: the small poster Plex builds, then the thumb itself."""
    return (
        (TRANSCODER_PATH,
         {"width": str(POSTER_WIDTH), "height": str(POSTER_HEIGHT), "minSize": "1",
          "upscale": "0", "url": thumb}),
        (thumb, None),
    )


def _fetch(media_item_id: int, thumb: str, settings) -> tuple[bytes, str] | None:
    """The image Plex answers with, or ``None`` for the placeholder (S5)."""
    if not (settings.plex_base_url and settings.plex_token):
        _warn(media_item_id, "plex_not_configured")
        return None
    client = PlexClient.from_settings(settings)
    reason = "no_image"
    for path, params in _attempts(thumb):
        try:
            body, content_type = client.image(path, params)
        except PlexError as error:
            reason = type(error).__name__
            continue
        if body and content_type in ACCEPTED_TYPES:
            return body, content_type
        reason = "not_an_image"
    _warn(media_item_id, reason)
    return None


def _warn(media_item_id: int, reason: str) -> None:
    # S2: never the token and never a URL that carries it; the thumb stays
    # out of the record too, and there is one warning per failed poster.
    logger.warning("poster_unavailable", extra={"media_item_id": media_item_id, "reason": reason})


def _image_response(body: bytes, content_type: str) -> Response:
    """S4: the real image, its real type, and a day of private caching."""
    response = Response(body, content_type=content_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
    return response


def _placeholder() -> Response:
    """S5: the neutral image, and nothing a cache may keep."""
    response = current_app.send_static_file(PLACEHOLDER_ASSET)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
