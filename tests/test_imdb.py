"""S2 (#153): one helper builds every IMDb address an item card shows.

A stored id becomes the item's title page; anything else becomes a search
for the item's own title and year, URL-encoded. A stored value that is not
an IMDb id is never used as an address, and with no id and no title there is
no link at all.
"""

from __future__ import annotations

import pytest

from almali_library_curator.imdb import imdb_url, is_imdb_id

TITLE_PAGE = "https://www.imdb.com/title/tt1375666/"
SEARCH = "https://www.imdb.com/find/"


# -- a stored id becomes the title page --------------------------------------

@pytest.mark.parametrize("imdb_id", ["tt1375666", "tt0000001", "tt1234567890"])
def test_a_stored_id_becomes_the_title_page(imdb_id):
    assert is_imdb_id(imdb_id)
    assert imdb_url(imdb_id, "Tenet", 2020) == f"https://www.imdb.com/title/{imdb_id}/"


def test_the_title_page_wins_over_the_title_and_the_year():
    assert imdb_url("tt1375666", "Another title", 1999) == TITLE_PAGE


# -- anything else becomes a search ------------------------------------------

@pytest.mark.parametrize("stored", [
    "https://evil.example/x",
    "tt123",
    "tt12345678901",
    "1375666",
    "ttabcdefg",
    "",
    None,
    1234567,
])
def test_anything_that_is_not_an_imdb_id_is_not_used_as_the_address(stored):
    assert not is_imdb_id(stored)
    url = imdb_url(stored, "Tenet", 2020)

    assert url == f"{SEARCH}?q=Tenet+2020&s=tt"
    assert not stored or str(stored) not in url.split("?")[0]


def test_without_an_id_the_search_names_the_title_and_the_year():
    assert imdb_url(None, "Tenet", 2020) == f"{SEARCH}?q=Tenet+2020&s=tt"


def test_an_unknown_year_searches_the_title_alone():
    assert imdb_url(None, "Tenet") == f"{SEARCH}?q=Tenet&s=tt"
    assert imdb_url(None, "Tenet", None) == f"{SEARCH}?q=Tenet&s=tt"


def test_the_query_is_url_encoded():
    """A title with ``&``, ``#`` and accents never breaks the address."""
    url = imdb_url(None, "Amélie & #1", 2001)

    assert url == f"{SEARCH}?q=Am%C3%A9lie+%26+%231+2001&s=tt"
    assert "&" not in url.split("?")[1].replace("&s=tt", "")
    assert "#" not in url
    assert "é" not in url
    assert url.startswith(f"{SEARCH}?q=")


def test_a_title_of_spaces_is_no_title():
    assert imdb_url(None, "   ", 2020) is None


def test_no_id_and_no_title_is_no_link():
    assert imdb_url() is None
    assert imdb_url(None, None, 2020) is None
    assert imdb_url(None, "", None) is None


def test_every_address_the_helper_builds_is_an_accepted_one():
    from almali_library_curator.imdb import imdb_url, is_imdb_url
    for built in (imdb_url('tt1375666'), imdb_url(None, 'Amélie & Co #1', 2001),
                  imdb_url(None, '1923'), imdb_url('tt0000001', 'x', 1)):
        assert is_imdb_url(built), built


@pytest.mark.parametrize('value', [
    None, '', 'javascript:alert(1)', 'https://evil.example/title/tt1375666/',
    'https://www.imdb.com.evil.example/title/tt1375666/', 'http://www.imdb.com/title/tt1375666/',
    'https://www.imdb.com/title/tt1375666/?ref=x', 'https://www.imdb.com/title/tt12/',
    'https://www.imdb.com/find/?q=a"b&s=tt', 'https://www.imdb.com/find/?q=&s=tt',
    'https://www.imdb.com/find/?q=Tenet&s=tt\n', 42,
])
def test_anything_else_is_not_an_accepted_address(value):
    from almali_library_curator.imdb import is_imdb_url
    assert not is_imdb_url(value)
