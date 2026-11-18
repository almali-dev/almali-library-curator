"""#110: static assets are versioned, compressed and cheaply revalidated.

The 548 kB of CSS and the JS used to revalidate on every page and travelled
uncompressed. The version token is the asset's own content digest, so a
release never serves a stale asset from a browser or a proxy cache.
"""

from pathlib import Path

import gzip

from flask import render_template_string

from almali_library_curator.app import asset_version

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
STATIC = ROOT / "static"
YEAR = 365 * 24 * 60 * 60


def version(app, filename):
    return asset_version(filename, app.static_folder)


def test_the_version_token_follows_the_asset_contents(tmp_path, settings):
    """A content change must change the token; a missing asset has none."""
    folder = tmp_path / "static"
    folder.mkdir()
    asset = folder / "app.css"
    asset.write_text("body { color: #ddeeff; }")
    first = asset_version("app.css", folder)
    assert first
    assert asset_version("app.css", folder) == first
    assert asset_version("js/missing.js", folder) is None
    asset.write_text("body { color: #00d2ff; }")
    assert asset_version("app.css", folder) not in (None, first)


def test_asset_url_emits_the_version_of_the_file_it_points_at(app):
    """The helper the templates call to version every static URL."""
    with app.test_request_context("/"):
        url = render_template_string('{{ asset_url("css/hud.css") }}')
        missing = render_template_string('{{ asset_url("js/missing.js") }}')
    assert url == f"/static/css/hud.css?v={version(app, 'css/hud.css')}"
    assert missing == "/static/js/missing.js"


def test_a_versioned_url_is_cached_for_a_year_and_immutably(client, app):
    response = client.get(f"/static/css/hud.css?v={version(app, 'css/hud.css')}")
    assert response.status_code == 200
    assert response.data
    cache = response.headers["Cache-Control"]
    assert f"max-age={YEAR}" in cache
    assert "immutable" in cache
    assert "public" in cache


def test_unversioned_or_stale_version_static_never_gets_the_immutable_cache(
    client, app
):
    plain = client.get("/static/css/hud.css")
    assert plain.status_code == 200
    assert "immutable" not in plain.headers.get("Cache-Control", "")
    assert "no-store" not in plain.headers.get("Cache-Control", "")
    stale = client.get("/static/css/hud.css?v=0000000000000000")
    assert stale.status_code == 200
    assert "immutable" not in stale.headers.get("Cache-Control", "")


def test_conditional_requests_still_answer_304_for_both_urls(client, app):
    plain = client.get("/static/css/hud.css")
    cached = client.get(
        "/static/css/hud.css", headers={"If-None-Match": plain.headers["ETag"]}
    )
    assert cached.status_code == 304
    versioned_url = f"/static/css/hud.css?v={version(app, 'css/hud.css')}"
    versioned = client.get(versioned_url)
    assert (
        client.get(
            versioned_url, headers={"If-None-Match": versioned.headers["ETag"]}
        ).status_code
        == 304
    )


def test_every_static_url_a_page_renders_is_versioned(client, app):
    """No page links an asset that would revalidate on every page."""
    import re

    seen = set()
    for path in ("/", "/login", "/settings", "/history"):
        body = client.get(path, follow_redirects=True).get_data(as_text=True)
        for match in re.finditer(r'/static/([^"\'?#\s]+)(\?v=([0-9a-f]+))?', body):
            filename, _, token = match.groups()
            seen.add(filename)
            assert token == version(app, filename), (path, filename)
    assert "css/hud.css" in seen
    assert "icons.svg" in seen


def test_static_responses_keep_the_security_headers(client, app):
    for headers in ({"Accept-Encoding": "gzip"}, {"Accept-Encoding": "identity"}):
        response = client.get(
            f"/static/css/hud.css?v={version(app, 'css/hud.css')}", headers=headers
        )
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'self'; frame-ancestors 'none'"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Type"].startswith("text/css")


def test_static_files_outside_the_folder_or_missing_answer_404(client):
    assert client.get("/static/js/missing.js").status_code == 404
    assert client.get("/static/../app.py").status_code == 404


def test_pages_still_forbid_any_storage(client):
    """The versioned static cache must not leak onto pages or APIs."""
    for path in ("/", "/login", "/healthz", "/history.csv"):
        assert client.get(path).headers["Cache-Control"] == "no-store"


def test_gzip_when_the_client_accepts_it(client):
    raw = (STATIC / "css/hud.css").read_bytes()
    response = client.get("/static/css/hud.css", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["Vary"]
    assert gzip.decompress(response.data) == raw
    assert len(response.data) < len(raw)


def test_plain_bytes_when_the_client_does_not_accept_gzip(client):
    response = client.get(
        "/static/css/hud.css", headers={"Accept-Encoding": "identity"}
    )
    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert response.data == (STATIC / "css/hud.css").read_bytes()


def test_the_compressed_variant_carries_its_own_validator(client):
    plain = client.get("/static/css/hud.css", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/static/css/hud.css", headers={"Accept-Encoding": "gzip"})
    assert plain.headers["ETag"] != zipped.headers["ETag"]
    again = client.get(
        "/static/css/hud.css",
        headers={"Accept-Encoding": "gzip", "If-None-Match": zipped.headers["ETag"]},
    )
    assert again.status_code == 304
    wrong = client.get(
        "/static/css/hud.css",
        headers={
            "Accept-Encoding": "identity",
            "If-None-Match": zipped.headers["ETag"],
        },
    )
    assert wrong.status_code == 200


def test_javascript_and_svg_are_compressed_too(client):
    for filename in ("js/chrome-disclosures.js", "icons.svg"):
        response = client.get(
            "/static/" + filename, headers={"Accept-Encoding": "gzip"}
        )
        assert response.status_code == 200, filename
        assert response.headers["Content-Encoding"] == "gzip", filename
        assert gzip.decompress(response.data) == (STATIC / filename).read_bytes(), (
            filename
        )


def test_assets_that_are_already_compressed_are_served_as_they_are(client):
    filename = "vendor/google-fonts/exo2-7cHmv4okm5zmbtYlK-4E4Q.woff2"
    response = client.get("/static/" + filename, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert response.data == (STATIC / filename).read_bytes()
