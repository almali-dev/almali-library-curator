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
    assert "<title>Almali Library Curator for Plex</title>" in response.get_data(as_text=True)


def test_index_page_shows_the_almali_family_and_the_plex_disclaimer(client):
    body = client.get("/").get_data(as_text=True)

    assert "Part of Almali" in body
    assert "https://" not in body and "http://" not in body
    assert "Not affiliated with or endorsed by Plex Inc." in body


def test_the_almali_brand_assets_are_served(client):
    mark = client.get("/static/almali-symbol-dark.svg")
    favicon = client.get("/static/favicon.svg")

    assert mark.status_code == 200
    assert "<svg" in mark.get_data(as_text=True)
    assert favicon.status_code == 200
    assert "<svg" in favicon.get_data(as_text=True)
