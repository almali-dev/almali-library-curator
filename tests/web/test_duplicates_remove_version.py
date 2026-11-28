"""#133 T7: removing one version from the Duplicates screen.

S1 (verbatim): "From the Duplicates view, the owner can remove one version
of an item (for example keep the 4K and remove the 1080p, or the other way
round) while the other versions stay. [...] The app suggests which version
to keep [...] and the owner confirms." S2: the suggestion is the highest
resolution. The button only marks the version: it goes through the grace
period and the final check like every removal.
"""

from __future__ import annotations

import re
from html import unescape

from almali_library_curator.i18n import translate
from test_duplicates import seed_library_row, seed_movie

FOUR_K = "/media/Movies/Heat/Heat 2160p.mkv"
HD = "/media/Movies/Heat/Heat 1080p.mkv"


def seed(conn):
    library = seed_library_row(conn, "1", "Movies")
    heat = seed_movie(conn, library, "11", "Heat", path=FOUR_K)
    for position, (media_id, path, resolution, codec, size) in enumerate(
            [(501, FOUR_K, "4k", "hevc", 40), (502, HD, "1080", "h264", 12)]):
        conn.execute(
            "INSERT INTO media_versions (media_item_id, position, plex_media_id, resolution,"
            " video_codec, size_bytes, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (heat, position, media_id, resolution, codec, size, path))
    conn.commit()
    return heat


def text_of(response):
    return unescape(" ".join(re.sub(r"<[^>]+>", " ", response.get_data(as_text=True)).split()))


def flashes(client):
    with client.session_transaction() as session:
        return [key for _, key in session.get("_flashes", [])]


def requests(conn):
    return [dict(r) for r in conn.execute(
        "SELECT media_item_id, version_media_id, version_file_path, state FROM removal_requests")]


def test_only_the_version_not_suggested_offers_removal(app, conn, client):
    heat = seed(conn)

    html = client.get("/duplicates?lang=en").get_data(as_text=True)

    forms = re.findall(r'<form[^>]+action="([^"]+)"', html)
    assert [f for f in forms if "/versions/" in f] == [f"/items/{heat}/versions/502/remove?lang=en"]
    assert translate("duplicates.suggested_keep", "en") in text_of(
        client.get("/duplicates?lang=en"))
    assert translate("action.remove_version", "en") in html


def test_removing_asks_first_and_names_both_files(app, conn, client):
    heat = seed(conn)

    response = client.post(f"/items/{heat}/versions/502/remove?lang=en",
                           data={"file_path": HD})

    assert response.status_code == 200
    page = text_of(response)
    assert HD in page and FOUR_K in page
    assert f"/items/{heat}/versions/502/remove/confirm" in response.get_data(as_text=True)
    assert requests(conn) == []


def test_confirming_marks_the_version_and_returns_to_duplicates(app, conn, client):
    heat = seed(conn)

    response = client.post(f"/items/{heat}/versions/502/remove/confirm?lang=en",
                           data={"file_path": HD})

    assert response.status_code == 303
    assert "/duplicates" in response.headers["Location"]
    assert flashes(client) == ["web.flash.version_marked"]
    assert requests(conn) == [{"media_item_id": heat, "version_media_id": 502,
                               "version_file_path": HD, "state": "marked"}]


def test_a_stale_page_is_refused_and_nothing_is_written(app, conn, client):
    heat = seed(conn)

    response = client.post(f"/items/{heat}/versions/502/remove/confirm?lang=en",
                           data={"file_path": "/media/Movies/Heat/other.mkv"})

    assert response.status_code == 303
    assert flashes(client) == ["web.flash.version_stale"]
    assert requests(conn) == []


def test_the_confirmation_page_refuses_a_version_that_is_not_stored(app, conn, client):
    heat = seed(conn)

    response = client.post(f"/items/{heat}/versions/999/remove?lang=en",
                           data={"file_path": HD})

    assert response.status_code == 303
    assert flashes(client) == ["web.flash.version_stale"]


def test_leaving_soon_shows_the_version_its_size_and_one_file(app, conn, client):
    heat = seed(conn)
    client.post(f"/items/{heat}/versions/502/remove/confirm?lang=en", data={"file_path": HD})

    page = text_of(client.get("/?tab=leaving_soon&lang=en"))

    from almali_library_curator.web.format import human_size

    assert "Heat · 1080 · h264" in page
    assert f"Size {human_size(12, 'en')}" in page
    assert "1 file" in page


def test_every_new_string_exists_in_every_language():
    for key in ("action.remove_version", "duplicates.suggested_keep",
                "confirm.remove_version.title", "confirm.remove_version.message",
                "web.flash.version_marked", "web.flash.version_stale"):
        for locale in ("en", "ca", "es"):
            assert translate(key, locale) != key, (key, locale)


def test_history_names_the_version(app, conn, client):
    heat = seed(conn)
    client.post(f"/items/{heat}/versions/502/remove/confirm?lang=en", data={"file_path": HD})
    [request_id] = [r[0] for r in conn.execute("SELECT id FROM removal_requests")]
    client.post(f"/requests/{request_id}/keep?lang=en")

    page = text_of(client.get("/history?lang=en"))

    assert "Heat · 1080 · h264" in page
