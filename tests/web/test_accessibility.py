"""S2 of v0.38.0 (#72): WCAG 2.1 AA at the shell level.

Five rules, each read from what the app actually serves:

1. every signed-in screen has its own ``<title>``, naming the page and the app;
2. each library checkbox carries its library name as its accessible name;
3. the wizard's two navigation landmarks have distinct labels;
4. section titles and buttons are at least 12 px, the input border reaches
   3:1 against its background, and ``.btn:focus-visible`` keeps a visible ring
   although Tabler zeroes it;
5. settings fields have short help text linked with ``aria-describedby``, and
   a refused save lists every invalid field in an ``role="alert"`` summary.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

from almali_library_curator.i18n import translate

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
HUD = ROOT / "static/css/hud.css"
TABLER = ROOT / "static/vendor/tabler/1.0.0-beta20/tabler.min.css"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}
CONTROL_TAGS = {"input", "select", "textarea"}


# -- page titles --------------------------------------------------------------


# Every screen a signed-in owner can open, with the labels its title must name.
SIGNED_IN_PAGES = (
    ("library_health", "/", ("nav.library_health",)),
    ("leaving_soon", "/?tab=leaving_soon", ("nav.leaving_soon",)),
    ("protected", "/?tab=protected", ("nav.protected",)),
    ("history", "/history", ("nav.history",)),
    ("connections", "/connections", ("nav.connections",)),
    ("settings", "/settings", ("nav.settings",)),
    ("about", "/about", ("nav.about",)),
    ("setup_plex", "/setup?step=plex", ("setup.title", "setup.step.plex")),
    ("setup_libraries", "/setup?step=libraries", ("setup.title", "setup.step.libraries")),
    ("setup_connections", "/setup?step=connections", ("setup.title", "setup.step.connections")),
)


def _title(html):
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match, html[:200]
    return " ".join(match.group(1).split())


@pytest.mark.parametrize("locale", ["en", "ca"])
def test_every_signed_in_page_names_itself_and_the_app_in_its_own_title(client, locale):
    titles = {}
    for name, path, labels in SIGNED_IN_PAGES:
        separator = "&" if "?" in path else "?"
        response = client.get(f"{path}{separator}lang={locale}")
        assert response.status_code == 200, (name, response.status_code)
        titles[name] = _title(response.get_data(as_text=True))
    for name, _, labels in SIGNED_IN_PAGES:
        title = titles[name]
        assert title, name
        # The app name closes every title, so a bookmark is always recognisable.
        assert translate("app.name", locale) in title, (name, title)
        assert title != translate("app.name", locale), name
        for label in labels:
            assert translate(label, locale) in title, (name, label, title)
    assert len(set(titles.values())) == len(titles), titles


# -- accessible names ---------------------------------------------------------


class _Document(HTMLParser):
    """Just enough of the tree to compute an accessible name per control."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.open = []
        self.ids = {}
        self.controls = []
        self.legends = []
        self.labels_for = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        node = {"tag": tag, "attrs": attrs, "parts": [], "controls": [], "legends": []}
        control = None
        if tag in CONTROL_TAGS:
            control = {"attrs": attrs, "label_text": "", "legend_text": ""}
            self.controls.append(control)
        for ancestor in self.open:
            if control is None:
                break
            if ancestor["tag"] == "label":
                ancestor["controls"].append(control)
            elif ancestor["tag"] == "legend":
                ancestor["legends"].append(control)
        if tag in VOID:
            self._close(node)
        else:
            self.open.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        while self.open:
            node = self.open.pop()
            self._close(node)
            if node["tag"] == tag:
                break

    def handle_data(self, data):
        for node in self.open:
            node["parts"].append(data)

    def _close(self, node):
        text = " ".join("".join(node["parts"]).split())
        identifier = node["attrs"].get("id")
        if identifier:
            self.ids[identifier] = text
        for control in node["controls"]:
            control["label_text"] = text
        for control in node["legends"]:
            control["legend_text"] = text
        if node["tag"] == "legend":
            self.legends.append(text)
        if node["tag"] == "label" and node["attrs"].get("for"):
            self.labels_for[node["attrs"]["for"]] = text


def _accessible_name(control, document):
    """aria-label, then aria-labelledby, then a for= label, then a wrapping label."""
    attrs = control["attrs"]
    if attrs.get("aria-label"):
        return attrs["aria-label"]
    if attrs.get("aria-labelledby"):
        return " ".join(document.ids.get(ref, "") for ref in attrs["aria-labelledby"].split())
    if attrs.get("id") and document.labels_for.get(attrs["id"]):
        return document.labels_for[attrs["id"]]
    return control["label_text"]


LIBRARIES = {"1": "Movies", "2": "Filmoteca"}


@pytest.mark.parametrize("path", ["/settings", "/setup?step=libraries"])
def test_every_library_checkbox_is_named_by_its_library(app, client, conn, path):
    from lifecycle_helpers import seed_library

    for key, title in LIBRARIES.items():
        seed_library(conn, key, title)
    document = _Document(client.get(path).get_data(as_text=True))
    # One group per library, announced by its own legend.
    assert set(LIBRARIES.values()) <= set(document.legends), document.legends
    seen = set()
    for control in document.controls:
        attrs = control["attrs"]
        if attrs.get("type") != "checkbox" or attrs.get("name") not in (
                "curated_libraries", "protected_libraries"):
            continue
        name = _accessible_name(control, document)
        assert LIBRARIES[attrs["value"]] in name, (path, attrs, name)
        seen.add((attrs["name"], attrs["value"]))
    assert seen == {(scope, key) for scope in ("curated_libraries", "protected_libraries")
                    for key in LIBRARIES}


# -- the wizard's navigation landmarks ---------------------------------------


@pytest.mark.parametrize("locale", ["en", "ca", "es"])
def test_the_wizard_navigation_landmarks_have_distinct_labels(client, locale):
    html = client.get(f"/setup?step=libraries&lang={locale}").get_data(as_text=True)
    labels = [dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
              for attrs in re.findall(r"<nav([^>]*)>", html)
              if "wizard-steps" in attrs or "wizard-nav" in attrs]
    assert len(labels) == 2, labels
    names = [label.get("aria-label", "") for label in labels]
    assert all(names), names
    assert len(set(names)) == 2, names
    assert set(names) == {translate("setup.steps", locale),
                          translate("setup.navigation", locale)}, names


# -- CSS: type size, control contrast and the focus ring ---------------------


def _rules(css):
    """(selector list, declarations) for every rule; the file holds no nesting."""
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(selectors, dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", body)))
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)]


def _declarations(rules, *selectors):
    """The declarations of the first rule whose selector list holds them all."""
    for rule_selectors, declarations in rules:
        parts = [part.strip() for part in rule_selectors.split(",")]
        if all(selector in parts for selector in selectors):
            return declarations
    raise AssertionError(" ".join(selectors))


def _px_size(declarations):
    """The used size of a ``font`` shorthand or a ``font-size``, in CSS pixels."""
    candidates = [declarations.get("font-size", "")]
    candidates += declarations.get("font", "").replace("/", " ").split()
    for value in candidates:
        match = re.fullmatch(r"([\d.]+)(rem|px|em)", value.strip())
        if match:
            amount = float(match.group(1))
            return amount * 16 if match.group(2) in ("rem", "em") else amount
    raise AssertionError(f"no font size in {declarations}")


def _declared_size(rules, selector):
    """The size of the last rule for ``selector`` that sets the type size."""
    for selectors, declarations in reversed(rules):
        if selector in [part.strip() for part in selectors.split(",")] and (
                "font" in declarations or "font-size" in declarations):
            return _px_size(declarations)
    raise AssertionError(f"no font size for {selector}")


@pytest.mark.parametrize("selector", [".section-title", ".btn"])
def test_titles_and_buttons_are_at_least_twelve_pixels(selector):
    assert _declared_size(_rules(HUD.read_text()), selector) >= 12


def _luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.03928
              else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_input_border_reaches_three_to_one_against_its_background():
    css = HUD.read_text()
    tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css))
    declarations = _declarations(_rules(css), "input", "select", "textarea")
    border = re.search(r"var\((--[\w-]+)\)|(#[0-9a-fA-F]{6})",
                       declarations.get("border") or declarations.get("border-color") or "")
    assert border, declarations
    color = tokens[border.group(1)] if border.group(1) else border.group(2)
    # The control sits on a card, and its own fill is the elevated surface.
    for background in ("--bg", "--elevated"):
        assert _contrast(color, tokens[background].strip()) >= 3.0, (color, background)


def test_the_button_focus_ring_is_explicit_and_wins_over_tabler(client):
    # Tabler zeroes the ring at the same specificity, so hud.css must restate it
    # and be the sheet the browser reads last.
    assert re.search(r"\.btn:focus-visible\{[^}]*outline:0", TABLER.read_text())
    ring = _declarations(_rules(HUD.read_text()), ".btn:focus-visible")
    assert not re.fullmatch(r"\s*(0|none)\s*", ring.get("outline", ""))
    width = re.fullmatch(r"\s*([\d.]+)px\s+\w+\s+.+", ring.get("outline", ""))
    assert width and float(width.group(1)) >= 2, ring
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"',
                        client.get("/").get_data(as_text=True))
    assert sheets.index("/static/css/hud.css") > max(
        index for index, href in enumerate(sheets) if "tabler.min.css" in href)


# -- settings help text and the error summary --------------------------------


def test_every_settings_field_explains_itself_through_aria_describedby(client, conn):
    from lifecycle_helpers import seed_library

    seed_library(conn, "1", "Movies")
    document = _Document(client.get("/settings").get_data(as_text=True))
    visible = [control for control in document.controls
               if control["attrs"].get("type") != "hidden"]
    assert visible
    for control in visible:
        attrs = control["attrs"]
        described = (attrs.get("aria-describedby") or "").split()
        assert described, attrs
        for reference in described:
            text = document.ids.get(reference, "")
            assert text, (attrs, reference)
            assert "settings." not in text, (reference, text)


def test_a_refused_settings_save_summarises_every_invalid_field(client, app):
    response = client.post("/settings", data={
        "grace_days": "2", "scan_time": "25:00", "collection_name": "   "})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    document = _Document(html)
    summary = re.search(r'<div class="error-summary" role="alert">(.*?)</div>', html, re.S)
    assert summary, html[:400]
    links = re.findall(r'<a href="#([^"]+)"[^>]*>([^<]+)</a>', summary.group(1))
    assert [href for href, _ in links] == [
        "field-grace_days", "field-collection_name", "field-scan_time"], links
    assert [label.strip() for _, label in links] == [
        translate("settings.grace_days", "en"),
        translate("settings.collection_name", "en"),
        translate("settings.scan_time", "en")]
    for href, _ in links:
        # The link lands on the control it names.
        assert href in document.ids, href
    for key in ("web.form.grace_range", "web.form.collection_required", "web.form.scan_time"):
        assert translate(key, "en") in summary.group(1), key
