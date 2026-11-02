from __future__ import annotations


def test_index_page_renders_in_the_requested_language(client):
    response = client.get("/?lang=ca")

    assert response.status_code == 200
    assert "Candidats" in response.get_data(as_text=True)


def test_index_page_falls_back_to_the_default_locale(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Candidates" in response.get_data(as_text=True)
