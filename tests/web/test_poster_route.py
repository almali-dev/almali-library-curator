"""The poster route: served by the app, cached on disk, never the token (#139).

Every case goes through HTTP: a signed-in ``client`` for the owner, a plain
``FlaskClient`` for the anonymous request, and the image fake server of
``poster_fakes`` for Plex. S2 (the token never reaches the browser), S3 (only
a stored Plex thumb is requested), S4 (a day of private caching, then disk)
and S5 (a placeholder that is never cached) are all here.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import socket

from flask.testing import FlaskClient
import pytest

from poster_fakes import (
    JPEG, OTHER_THUMB, OVER_CAP, PNG, THUMB, TRANSCODER, FakePosterPlexServer,
)

from almali_library_curator.app import create_app
from almali_library_curator.config import save_settings
from almali_library_curator.db import connect
from almali_library_curator.plex import PlexClient

TOKEN = "plex-poster-token-5f21c0ffee"
CSP = "default-src 'self'; frame-ancestors 'none'"
IMAGE_CACHE = "private, max-age=86400"

# S3: values a stored thumb must never become a Plex request.
INVALID_THUMBS = [
    "https://evil.example/x.jpg",
    "/library/metadata/11/thumb/1700000000?X-Plex-Token=stolen",
    "/library/metadata/11/thumb/../../etc/passwd",
]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """The route builds the client from settings, so its retries carry the
    client's own 1 s backoff; the tests wait for none of it."""
    monkeypatch.setattr(PlexClient, "_backoff", lambda self, attempt: None)


@pytest.fixture()
def plex():
    with FakePosterPlexServer(token=TOKEN) as server:
        server.serve()
        yield server


def build_app(settings, base_url, owner_client_class):
    """The app pointed at one Plex address, with a signed-in test client."""
    config = replace(settings, scheduler_enabled=False, plex_base_url=base_url,
                     plex_rate_limit_per_second=1000)
    save_settings(config)
    app = create_app(config)
    app.config.update(TESTING=True, SECRET_KEY="poster-test-session", WTF_CSRF_ENABLED=False)
    app.test_client_class = owner_client_class
    return app


@pytest.fixture()
def posters(settings, owner_client_class, monkeypatch, plex):
    monkeypatch.setenv("ALC_PLEX_TOKEN", TOKEN)
    monkeypatch.delenv("ALC_PLEX_BASE_URL", raising=False)
    return build_app(settings, plex.base_url, owner_client_class)


@pytest.fixture()
def client(posters):
    return posters.test_client()


@pytest.fixture()
def db(posters):
    conn = connect(posters.config["SETTINGS"].database_path)
    yield conn
    conn.close()


def add_item(db, *, thumb=THUMB, plex_rating_key="11", title="Tenet"):
    """One synced movie, with the thumb the sync stored."""
    library_id = db.execute(
        "INSERT OR IGNORE INTO libraries (plex_key, title, section_type) VALUES ('1', 'Movies', 'movie')"
    ).lastrowid
    library_id = library_id or db.execute(
        "SELECT id FROM libraries WHERE plex_key = '1'"
    ).fetchone()[0]
    identifier = db.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type, thumb)"
        " VALUES (?, ?, ?, 'movie', ?)",
        (library_id, plex_rating_key, title, thumb),
    ).lastrowid
    db.commit()
    return identifier


def add_scan_row(db, media_item_id, thumb=THUMB):
    """A published snapshot row, which is where a dry run's thumb lives."""
    run_id = db.execute(
        "INSERT INTO scan_runs (status, mode) VALUES ('finished', 'dry_run')"
    ).lastrowid
    db.execute(
        "INSERT INTO scan_rows (run_id, kind, position, media_item_id, unit, seasons, title,"
        " type_badge, categories, reasons, reason, size_bytes, thumb)"
        " VALUES (?, 'candidate', 0, ?, 'movie', '[]', 'Tenet', 'MOVIE', '[]', '[]',"
        " 'Added January 2024.', 1, ?)",
        (run_id, media_item_id, thumb),
    )
    db.commit()
    return run_id


def poster_files(config_dir):
    directory = Path(config_dir) / "posters"
    return sorted(directory.iterdir()) if directory.is_dir() else []


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def assert_placeholder(response):
    """S5: the neutral image, never cached, never a broken image."""
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    assert response.headers["Cache-Control"] == "no-store"
    assert b"<svg" in response.data


# -- S2: behind the owner login ----------------------------------------------

def test_an_anonymous_request_is_sent_to_the_login(posters, db, plex):
    add_item(db)
    response = FlaskClient(posters).get("/poster/1")
    assert response.status_code == 303
    assert response.headers["Location"].startswith("/login")
    assert plex.requests == []


# -- S3 and S5: no thumb, or one that is not a Plex poster path, is a 404 ----

def test_an_unknown_item_is_a_404_and_asks_plex_nothing(client, plex):
    assert client.get("/poster/4242").status_code == 404
    assert plex.requests == []


def test_an_item_without_a_thumb_is_a_404_and_asks_plex_nothing(client, db, plex):
    identifier = add_item(db, thumb=None)
    assert client.get(f"/poster/{identifier}").status_code == 404
    assert plex.requests == []


@pytest.mark.parametrize("thumb", INVALID_THUMBS)
def test_a_stored_thumb_that_is_not_a_poster_path_is_a_404(client, db, plex, thumb):
    identifier = add_item(db, thumb=thumb)
    assert client.get(f"/poster/{identifier}").status_code == 404
    assert plex.requests == []


# -- S4: a day of private caching, and the token never in the answer ---------

def test_a_stored_thumb_is_served_as_an_image_of_the_size_asked_for(client, db, plex):
    identifier = add_item(db)
    response = client.get(f"/poster/{identifier}")

    assert response.status_code == 200
    assert response.data == PNG
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == IMAGE_CACHE

    # The transcoder, once, with the size and the thumb it was asked for.
    assert len(plex.requests) == 1
    request = plex.requests[0]
    assert request["path"] == TRANSCODER
    assert request["headers"]["Accept"] == "image/*"
    assert request["query"]["width"] == "240"
    assert request["query"]["height"] == "360"
    assert request["query"]["minSize"] == "1"
    assert request["query"]["upscale"] == "0"
    assert request["query"]["url"] == THUMB

    # S2: the token travels as a header, and never reaches the browser.
    assert request["headers"]["X-Plex-Token"] == TOKEN
    assert "X-Plex-Token" not in request["query"]
    assert TOKEN not in request["raw_path"]
    assert TOKEN.encode() not in response.data
    for value in response.headers.values():
        assert TOKEN not in value
        assert plex.base_url not in value


def test_the_second_request_is_served_from_the_disk(client, db, posters, plex):
    identifier = add_item(db)
    first = client.get(f"/poster/{identifier}")
    requests_after_first = len(plex.requests)

    second = client.get(f"/poster/{identifier}")

    assert second.status_code == 200 and second.data == first.data
    assert len(plex.requests) == requests_after_first
    files = poster_files(posters.config["SETTINGS"].config_dir)
    assert len(files) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\.png", files[0].name)


def test_a_cached_image_survives_plex_going_away(client, db, plex):
    identifier = add_item(db)
    client.get(f"/poster/{identifier}")
    plex.fail_all = 500
    requests_before = len(plex.requests)

    response = client.get(f"/poster/{identifier}")

    assert response.status_code == 200 and response.data == PNG
    assert len(plex.requests) == requests_before


def test_two_thumbs_do_not_share_a_cache_file(client, db, posters, plex):
    first = add_item(db, plex_rating_key="11", thumb=THUMB)
    second = add_item(db, plex_rating_key="12", thumb=OTHER_THUMB, title="Heat")
    plex.serve(OTHER_THUMB, content_type="image/jpeg", body=JPEG)

    assert client.get(f"/poster/{first}").data == PNG
    assert client.get(f"/poster/{second}").data == JPEG

    names = [path.name for path in poster_files(posters.config["SETTINGS"].config_dir)]
    assert len(names) == 2
    assert names[0] != names[1]
    assert sorted(name.rsplit(".", 1)[1] for name in names) == ["jpg", "png"]


def test_the_thumb_of_a_dry_run_comes_from_the_snapshot_row(client, db, plex):
    identifier = add_item(db, thumb=None)
    add_scan_row(db, identifier)

    response = client.get(f"/poster/{identifier}")

    assert response.status_code == 200 and response.data == PNG
    assert plex.requests[0]["query"]["url"] == THUMB


# -- S5: the transcoder that says no, and the raw thumb ----------------------

def test_a_transcoder_that_does_not_have_the_image_falls_back_to_the_thumb(client, db, plex):
    identifier = add_item(db)
    plex.statuses[TRANSCODER] = 404

    response = client.get(f"/poster/{identifier}")

    assert response.status_code == 200 and response.data == PNG
    assert [request["path"] for request in plex.requests] == [TRANSCODER, THUMB]


# -- S5: the placeholder, and nothing written to the disk --------------------

def test_a_plex_error_serves_the_placeholder_and_retries_later(client, db, posters, plex):
    identifier = add_item(db)
    plex.fail_all = 500

    first = client.get(f"/poster/{identifier}")
    assert_placeholder(first)
    assert poster_files(posters.config["SETTINGS"].config_dir) == []
    requests_after_first = len(plex.requests)

    second = client.get(f"/poster/{identifier}")
    assert_placeholder(second)
    assert len(plex.requests) > requests_after_first


def test_an_unreachable_plex_serves_the_placeholder(settings, owner_client_class,
                                                   monkeypatch, db):
    monkeypatch.setenv("ALC_PLEX_TOKEN", TOKEN)
    monkeypatch.delenv("ALC_PLEX_BASE_URL", raising=False)
    app = build_app(settings, closed_port_url(), owner_client_class)
    identifier = add_item(db)

    response = app.test_client().get(f"/poster/{identifier}")

    assert_placeholder(response)
    assert poster_files(app.config["SETTINGS"].config_dir) == []


@pytest.mark.parametrize("content_type,body", [("text/html", b"<html>not an image</html>"),
                                               ("image/png", OVER_CAP)])
def test_an_answer_that_is_not_a_small_image_serves_the_placeholder(
    client, db, posters, plex, content_type, body
):
    identifier = add_item(db)
    plex.images[TRANSCODER] = (content_type, body)
    plex.images[THUMB] = (content_type, body)

    response = client.get(f"/poster/{identifier}")

    assert_placeholder(response)
    assert poster_files(posters.config["SETTINGS"].config_dir) == []
    # Both attempts happened: the transcoder, then the raw thumb.
    assert [request["path"] for request in plex.requests] == [TRANSCODER, THUMB]


def test_a_redirect_from_plex_is_not_followed(client, db, posters, plex):
    identifier = add_item(db)
    plex.redirects[TRANSCODER] = "/library/metadata/99/thumb/1"
    plex.redirects[THUMB] = "/library/metadata/99/thumb/1"

    response = client.get(f"/poster/{identifier}")

    assert_placeholder(response)
    assert poster_files(posters.config["SETTINGS"].config_dir) == []
    assert [request["path"] for request in plex.requests] == [TRANSCODER, THUMB]


def test_without_plex_configured_the_placeholder_is_served(settings, owner_client_class,
                                                           monkeypatch, db):
    monkeypatch.delenv("ALC_PLEX_TOKEN", raising=False)
    monkeypatch.delenv("ALC_PLEX_BASE_URL", raising=False)
    app = build_app(settings, "", owner_client_class)
    identifier = add_item(db)

    response = app.test_client().get(f"/poster/{identifier}")

    assert_placeholder(response)
    assert poster_files(app.config["SETTINGS"].config_dir) == []


def test_one_warning_per_failed_poster_and_no_token_in_it(client, db, plex, caplog):
    identifier = add_item(db)
    plex.fail_all = 500

    with caplog.at_level("WARNING"):
        client.get(f"/poster/{identifier}")

    mine = [record for record in caplog.records
            if record.name == "almali_library_curator.web.posters"]
    assert len(mine) == 1
    assert TOKEN not in mine[0].getMessage()
    assert TOKEN not in str(mine[0].__dict__)
    assert THUMB not in mine[0].getMessage()


# -- S2: the CSP is unchanged, and the rest of the app keeps no-store --------

@pytest.mark.parametrize("outcome", ["image", "placeholder"])
def test_the_poster_keeps_the_app_csp(client, db, plex, outcome):
    identifier = add_item(db)
    if outcome == "placeholder":
        plex.fail_all = 503

    response = client.get(f"/poster/{identifier}")

    assert response.headers.getlist("Content-Security-Policy") == [CSP]
    assert response.headers.getlist("X-Frame-Options") == ["DENY"]


def test_the_other_pages_keep_the_app_wide_no_store(client):
    for path in ("/", "/history", "/duplicates"):
        assert client.get(path).headers["Cache-Control"] == "no-store"
