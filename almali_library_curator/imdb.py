"""The IMDb address of an item card (#153).

One helper builds every IMDb link the screens show: a stored id becomes the
item's title page, and a title (with its year, when known) becomes a search.
Nothing else is ever rendered as an href, so a stored value can never point
the card anywhere else.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

#: The IMDb title page of a stored id.
TITLE_PAGE = "https://www.imdb.com/title/{imdb_id}/"

#: IMDb's own search, the fallback when the item carries no id.
SEARCH = "https://www.imdb.com/find/"

#: What Plex reports for an IMDb id, and the only shape used as an address.
_IMDB_ID = re.compile(r"tt[0-9]{7,10}")

#: The two addresses :func:`imdb_url` builds, and nothing else: a title page,
#: or a search whose query is exactly what ``urlencode`` writes.
_IMDB_ADDRESS = re.compile(
    r"https://www\.imdb\.com/title/tt[0-9]{7,10}/"
    r"|https://www\.imdb\.com/find/\?q=[A-Za-z0-9%+._~-]+&s=tt")


def is_imdb_id(value: object) -> bool:
    """True when ``value`` is an IMDb id the title page may be built from."""
    return isinstance(value, str) and _IMDB_ID.fullmatch(value) is not None


def is_imdb_url(value: object) -> bool:
    """True when ``value`` is an address :func:`imdb_url` could have built."""
    return isinstance(value, str) and _IMDB_ADDRESS.fullmatch(value) is not None


def safe_imdb_url(value: object) -> str | None:
    """The address to render as a link, or ``None``.

    The Library's rows carry the address a scan stored; the screens render it
    only when it is still one the helper builds, so a stored value can never
    become a link to anywhere else (S2).
    """
    return value if is_imdb_url(value) else None


def imdb_url(imdb_id: object = None, title: object = None, year: object = None) -> str | None:
    """The IMDb address of an item, or ``None`` when it has neither id nor title.

    A stored id is used only when it is really an IMDb id; otherwise the
    address is a search for the item's own title and year, URL-encoded. A
    title that is missing or blank is no title, and a bare year is never a
    search of its own.
    """
    if is_imdb_id(imdb_id):
        return TITLE_PAGE.format(imdb_id=imdb_id)
    query = str(title).strip() if isinstance(title, str) else ""
    if not query:
        return None
    if year is not None and year != "":
        query = f"{query} {year}"
    return f"{SEARCH}?{urlencode({'q': query, 's': 'tt'})}"
