"""#162 S2: the Rotten Tomatoes address of a card, built and checked by the app."""

from __future__ import annotations

import pytest

from almali_library_curator.rottentomatoes import is_rt_url, rt_url, safe_rt_url


def test_a_title_becomes_a_rotten_tomatoes_search():
    assert rt_url("Tenet") == "https://www.rottentomatoes.com/search?search=Tenet"


def test_the_title_is_encoded():
    assert rt_url("Amélie & Co #1") == (
        "https://www.rottentomatoes.com/search?search=Am%C3%A9lie+%26+Co+%231")


@pytest.mark.parametrize("title", [None, "", "   ", 42])
def test_no_title_is_no_address(title):
    assert rt_url(title) is None


def test_every_address_the_helper_builds_is_accepted():
    for title in ("Tenet", "Amélie & Co #1", "1923", "Star Wars: The Clone Wars"):
        assert is_rt_url(rt_url(title))
        assert safe_rt_url(rt_url(title)) == rt_url(title)


@pytest.mark.parametrize("value", [
    None, "", "javascript:alert(1)", "https://evil.example/search?search=Tenet",
    "https://www.rottentomatoes.com.evil.example/search?search=Tenet",
    "http://www.rottentomatoes.com/search?search=Tenet",
    "https://www.rottentomatoes.com/search?search=",
    'https://www.rottentomatoes.com/search?search=a"b',
    "https://www.rottentomatoes.com/m/tenet", 42,
])
def test_anything_else_is_not_an_accepted_address(value):
    assert not is_rt_url(value)
    assert safe_rt_url(value) is None
