"""W9b's shell contract, independent of the screen/read-model integration."""
from __future__ import annotations

from dataclasses import replace
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from flask import flash, render_template_string, request
import pytest

from almali_library_curator.app import create_app
from almali_library_curator.i18n import translate

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
TABLER = "vendor/tabler/1.0.0-beta20/"
ASSETS = [TABLER + name for name in (
    "tabler.min.css", "tabler-vendors.min.css", "tabler.min.js", "LICENSE",
)] + ["css/hud.css", "icons.svg"]


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture()
def app(settings):
    app = create_app(replace(settings, scheduler_enabled=False))
    app.config.update(TESTING=True, SECRET_KEY="ui-test-only")
    return app


@pytest.mark.parametrize("asset", ASSETS)
def test_assets_are_served_locally(app, asset):
    response = app.test_client().get("/static/" + asset)
    assert response.status_code == 200
    assert response.data


def test_no_external_template_or_css_resources():
    paths = list((ROOT / "templates").rglob("*.html"))
    paths += list((ROOT / "static").rglob("*.css"))
    for path in paths:
        # Licence/source comments and inert data-URI SVGs are not requests.
        text = re.sub(r"/\*.*?\*/|<!--.*?-->|\{#.*?#\}", "", path.read_text(), flags=re.S)
        if path.suffix == ".css":
            resources = re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", text)
            resources += re.findall(r"@import\s+['\"]([^'\"]+)", text)
        else:
            resources = [value for _, attrs in Markup(text).elements
                         for key, value in attrs.items() if key in {"href", "src", "action"}]
        assert not [url for url in resources if re.match(r"(?:https?:)?//", url)], path


@pytest.mark.parametrize("locale", ["en", "ca", "es"])
@pytest.mark.parametrize("active,path", [
    ("health", "/"), ("history", "/history"), ("duplicates", "/duplicates"),
    ("connections", "/connections"), ("settings", "/settings"),
])
def test_shell_locales_navigation_slots_and_flash(app, locale, active, path):
    with app.test_request_context(path + "?lang=" + locale + "&outcome=deleted"):
        app.preprocess_request()
        flash(translate("health.ready", locale), "success")
        html = render_template_string(
            '{% extends "base.html" %}'
            '{% block sources %}<p id="source-slot">{{ t("health.ready") }}</p>{% endblock %}'
            '{% block last_scan %}<time>06:00</time>{% endblock %}',
            active_nav=active,
        )
    parsed = Markup(html)
    assert ('html', {'lang': locale, 'data-bs-theme': 'dark'}) in parsed.elements
    links = [attrs for tag, attrs in parsed.elements if tag == "a" and "data-nav" in attrs]
    assert {link["data-nav"] for link in links} == {
        "health", "history", "duplicates", "connections", "settings"}
    assert {link["href"].split("?")[0] for link in links} == {
        "/", "/history", "/duplicates", "/connections", "/settings"}
    assert [link["data-nav"] for link in links if link.get("aria-current") == "page"] == [active]
    for key in ("nav.library_health", "nav.history", "nav.duplicates", "nav.connections",
                "nav.settings", "shell.header_line", "footer.disclaimer", "locale.label"):
        assert translate(key, locale) in "".join(parsed.text)
    assert 'id="source-slot"' in html and '<time>06:00</time>' in html
    assert 'role="status"' in html and translate("health.ready", locale) in html
    for language in ("en", "ca", "es"):
        switch = next(attrs for tag, attrs in parsed.elements
                      if tag == "a" and attrs.get("hreflang") == language)
        assert "lang=" + language in switch["href"]
        assert "outcome=deleted" in switch["href"]
    assert "Unraid" not in html


def test_locale_switch_links_keep_the_mount_prefix(app):
    with app.test_request_context("/history?lang=ca", base_url="http://localhost/curator"):
        assert request.script_root == "/curator"
        app.preprocess_request()
        html = render_template_string('{% extends "base.html" %}')
    switches = [attrs["href"] for tag, attrs in Markup(html).elements
                if tag == "a" and attrs.get("hreflang")]
    assert [href.split("?")[0] for href in switches] == ["/curator/history"] * 3


def test_sprite_has_all_required_symbols(app):
    xml = ET.fromstring(app.test_client().get("/static/icons.svg").data)
    assert {node.attrib["id"] for node in xml if node.tag.endswith("symbol")} >= {
        "series", "seasons", "movie", "star", "shield", "clock", "trash", "check", "warning", "power",
    }


@pytest.mark.parametrize("macro,values", [
    ("type_badge", ["series", "seasons", "movie", "unrecognized"]),
    ("state_badge", ["candidate", "marked", "leaving_soon", "protected", "deleted",
                     "cancelled", "removal_failed", "dry_run", "unreadable", "unrecognized"]),
])
def test_every_badge_has_local_icon_and_translated_text(app, macro, values):
    for locale in ("en", "ca", "es"):
        for value in values:
            with app.test_request_context("/?lang=" + locale):
                app.preprocess_request()
                html = render_template_string(
                    '{% from "macros/ui.html" import ' + macro + ' with context %}'
                    '{{ ' + macro + '(value) }}', value=value,
                )
            parsed = Markup(html)
            assert any(tag == "svg" and attrs.get("aria-hidden") == "true" for tag, attrs in parsed.elements)
            assert any(tag == "use" and re.match(r"/static/icons\.svg\?v=[0-9a-f]+#", attrs["href"]) for tag, attrs in parsed.elements)
            text = "".join(parsed.text).strip()
            assert text and not text.startswith(("badge.", "state."))
            prefix = "badge." if macro == "type_badge" else "state."
            expected = value if value != "unrecognized" else "unknown"
            assert text == translate(prefix + expected, locale)


def test_macros_render_counts_candidate_details_and_safe_post_actions(app):
    with app.test_request_context("/"):
        app.preprocess_request()
        html = render_template_string('''
            {% from "macros/ui.html" import summary_card, tab_bar, category_filters,
                candidate_row, empty_state with context %}
            {{ summary_card('summary.candidates', '433 GB', '7', 'trash') }}
            {{ tab_bar(tabs, 'candidates') }}
            {{ category_filters(filters, 'all') }}
            {{ candidate_row(item, actions) }}
            {{ empty_state('empty.candidates', 'shield') }}
        ''', tabs=[dict(id="candidates", label_key="nav.candidates", count=7, href="/?tab=candidates")],
            filters=[dict(id="all", label_key="category.all", count=7, href="/?category=all")],
            item=dict(kind="seasons", title="Lost <Space> S2–S3", reason="No play <yet>.",
                      size="142 GB", disk="disk3", rating="8.2", rating_source="IMDb"),
            actions=[dict(label_key="action.keep_forever", url="/keep-forever", fields={"item_id": "12"}),
                     dict(label_key="action.mark", url="/mark", fields={"item_id": "12"})])
    assert "433 GB" in html and "142 GB" in html and "disk3" in html and "IMDb" in html
    assert "Lost &lt;Space&gt; S2–S3" in html and "No play &lt;yet&gt;." in html
    parsed = Markup(html)
    assert sum(tag == "form" and attrs.get("method") == "post" for tag, attrs in parsed.elements) == 2
    assert sum(tag == "input" and attrs.get("name") == "item_id" for tag, attrs in parsed.elements) == 2
    assert any(tag == "use" and attrs["href"].endswith("#star") for tag, attrs in parsed.elements)
    assert any(attrs.get("aria-current") == "page" for _, attrs in parsed.elements)
    assert "Keep forever" in html and "Mark for removal" in html


def test_hud_responsive_tokens_and_reduced_motion():
    css = (ROOT / "static/css/hud.css").read_text()
    for token in ("#080b12", "#0c1018", "#111620", "#ddeeff", "#00d2ff", "--font-ui", "--font-mono"):
        assert token in css
    assert "min-width: 768px" in css and "min-width: 992px" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "min-height: 44px" in css and "min-width: 44px" in css
    assert "minmax(0, 1fr)" in css and "overflow-wrap: anywhere" in css


def test_templates_have_no_literal_user_facing_copy():
    for path in (ROOT / "templates").rglob("*.html"):
        template = re.sub(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", "", path.read_text(), flags=re.S)
        assert not re.search(r"[^\W\d_]", "".join(Markup(template).text)), path


def test_layout_loads_styles_in_order_and_default_active_nav(app):
    with app.test_request_context("/history?lang=es"):
        app.preprocess_request()
        parsed = Markup(render_template_string('{% extends "base.html" %}'))
    sheets = [attrs["href"].split("?v=")[0] for tag, attrs in parsed.elements
              if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert sheets == ["/static/" + path for path in (
        TABLER + "tabler.min.css", TABLER + "tabler-vendors.min.css",
        "vendor/google-fonts/fonts.css", "css/hud.css",
    )]
    assert any(tag == "a" and attrs.get("data-nav") == "history"
               and attrs.get("aria-current") == "page" for tag, attrs in parsed.elements)
    # Every font resource is a real local file served by this app.
    fonts = (ROOT / "static/vendor/google-fonts/fonts.css").read_text()
    for filename in set(re.findall(r"url\(\./([^)]+)\)", fonts)):
        response = app.test_client().get("/static/vendor/google-fonts/" + filename)
        assert response.status_code == 200 and response.data.startswith(b"wOF2")
    license_text = (ROOT / "static/vendor/google-fonts/LICENSE").read_text()
    assert "Exo 2" in license_text and "Share Tech Mono" in license_text
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_unknown_facts_escape_actions_and_include_csrf_when_available(app):
    with app.test_request_context("/"):
        app.preprocess_request()
        html = render_template_string(
            '{% from "macros/ui.html" import candidate_row with context %}'
            '{{ candidate_row(item, actions) }}',
            item=dict(kind="movie", title="Title", reason="Reason", size="0 B",
                      disk=None, rating=None, rating_source=None),
            actions=[dict(label_key="action.undo", url="/undo", fields={"item_id": '<script>'})],
            csrf_token=lambda: "test-csrf-token",
        )
    assert html.count("Unknown") == 2 and "None" not in html
    assert '<input type="hidden" name="csrf_token" value="test-csrf-token"' in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_all_catalog_keys_and_format_fields_match():
    catalogs = {locale: json.loads((ROOT / f"i18n/{locale}.json").read_text()) for locale in ("en", "ca", "es")}
    assert "shell.header_line" in catalogs["en"]
    for locale, catalog in catalogs.items():
        assert catalog.keys() == catalogs["en"].keys(), locale
        for key, text in catalog.items():
            assert isinstance(text, str) and text.strip(), (locale, key)
            assert set(re.findall(r"\{\w+\}", text)) == set(re.findall(r"\{\w+\}", catalogs["en"][key])), (locale, key)


def test_the_active_category_filter_has_a_shape_cue_besides_colour(app):
    # S7: colour is never the only cue. The active filter carries a check
    # icon; the other filters carry none, so the state reads without colour.
    for locale in ("en", "ca", "es"):
        with app.test_request_context("/?lang=" + locale):
            app.preprocess_request()
            html = render_template_string(
                '{% from "macros/ui.html" import category_filters with context %}'
                '{{ category_filters(filters, "never_watched") }}',
                filters=[dict(id="all", label_key="category.all", count=7, href="/?category=all"),
                         dict(id="never_watched", label_key="category.never_watched", count=3,
                              href="/?category=never_watched")],
            )
        links = html.split("</a>")[:2]
        inactive, active = links
        assert 'aria-current="page"' in active and "#check" in active, locale
        assert 'aria-current' not in inactive and "<svg" not in inactive, locale
