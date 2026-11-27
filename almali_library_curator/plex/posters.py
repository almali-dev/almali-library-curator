"""Which stored Plex thumbs may become a poster request (#139).

Plex sends an item's poster as a relative path such as
``/library/metadata/11/thumb/1700000000``. Only that exact shape is ever
requested from Plex: anything else (an absolute URL, a query string, ``..``
or another endpoint) is treated as "no poster", so a stored value can never
turn the poster route into a request to an arbitrary place.
"""

from __future__ import annotations

import re

_POSTER_THUMB = re.compile(r"/library/metadata/[0-9]+/thumb/[0-9]+")


def is_poster_thumb(thumb: object) -> bool:
    """True when ``thumb`` is a Plex poster path the app may request."""
    return isinstance(thumb, str) and _POSTER_THUMB.fullmatch(thumb) is not None
