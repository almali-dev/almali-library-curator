from __future__ import annotations


def test_index_page_renders_in_the_requested_language(client):
    response = client.get("/?lang=ca")

    assert response.status_code == 200
    assert "Estat de la biblioteca" in response.get_data(as_text=True)


def test_index_page_falls_back_to_the_default_locale(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Library health" in response.get_data(as_text=True)


def test_index_page_shows_the_almali_visible_name(client):
    response = client.get("/")

    assert response.status_code == 200
    # v0.38.0 S2 (#72): the shell title names the screen, then the app.
    assert "<title>Library health · Almali Library Curator for Plex</title>" in response.get_data(as_text=True)


def test_index_page_shows_the_almali_family_and_the_plex_disclaimer(client):
    body = client.get("/").get_data(as_text=True)

    assert "Part of Almali" in body
    assert "https://" not in body and "http://" not in body
    assert "Not affiliated with or endorsed by Plex Inc." in body


def test_the_library_curator_brand_assets_are_served(client):
    """The header mark and the favicon are the Library Curator symbol from the Almali brand kit."""
    page = client.get("/").get_data(as_text=True)
    mark = client.get("/static/library-curator-symbol-dark.svg")
    favicon = client.get("/static/favicon.svg")

    assert 'src="/static/library-curator-symbol-dark.svg"' in page
    assert "almali-symbol-dark.svg" not in page
    assert mark.status_code == 200
    assert 'aria-label="Almali Library Curator"' in mark.get_data(as_text=True)
    assert favicon.status_code == 200
    assert 'aria-label="Almali Library Curator"' in favicon.get_data(as_text=True)
    assert client.get("/static/almali-symbol-dark.svg").status_code == 404
