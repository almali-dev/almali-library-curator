"""The Rotten Tomatoes address of an item card (#162).

Plex sends no Rotten Tomatoes id, so the card links to a Rotten Tomatoes
search for the item's own title. One helper builds that address, and the
screens render a stored one only when it still has exactly that shape.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

SEARCH = "https://www.rottentomatoes.com/search"

#: The only address :func:`rt_url` builds: a search whose query is exactly
#: what ``urlencode`` writes.
_RT_ADDRESS = re.compile(r"https://www\.rottentomatoes\.com/search\?search=[A-Za-z0-9%+._~-]+")


def rt_url(title: object) -> str | None:
    """The Rotten Tomatoes search for a title, or ``None`` without one."""
    query = title.strip() if isinstance(title, str) else ""
    if not query:
        return None
    return f"{SEARCH}?{urlencode({'search': query})}"


def is_rt_url(value: object) -> bool:
    """True when ``value`` is an address :func:`rt_url` could have built."""
    return isinstance(value, str) and _RT_ADDRESS.fullmatch(value) is not None


def safe_rt_url(value: object) -> str | None:
    """The address to render as a link, or ``None`` (S2)."""
    if isinstance(value, str) and is_rt_url(value):
        return value
    return None
