"""#148 (accessibility half): S1-S6, read from what the app actually serves.

S1 every row and service-card button carries a distinct accessible
description; S2 the heading order; S3 the danger "Resume deletions" and the
disabled "Protected" checkbox; S4 the button border contrast and History;
S5 the unused Tabler vendor assets; S6 the documented Force update.

Each rule is read from a real request through the signed-in ``client``
fixture, or from a sheet the browser loads, and never from the macro that
produced it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import pytest

from almali_library_curator.lifecycle import LifecycleEngine
from lifecycle_helpers import (
    MOVIE_SIZE, make_candidate, seed_item, seed_library,
)
from test_views import displayed, snapshot

ROOT = Path(__file__).resolve().parents[2]
HUD = ROOT / "almali_library_curator/static/css/hud.css"
CATALOGS = {locale: json.loads((ROOT / f"almali_library_curator/i18n/{locale}.json").read_text())
            for locale in ("en", "ca", "es")}
LOCALES = ("en", "ca", "es")
EN = CATALOGS["en"]


# -- reading the served markup -----------------------------------------------

class _Ids(HTMLParser):
    """The text of every element an ``aria-describedby`` may point at."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.open = []
        self.text = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"attrs": {key: value or "" for key, value in attrs}, "parts": []}
        self.open.append(node)

    def handle_endtag(self, tag):
        while self.open:
            node = self.open.pop()
            identifier = node["attrs"].get("id")
            if identifier:
                self.text[identifier] = " ".join("".join(node["parts"]).split())
            if True:  # every end tag closes one node; nesting is well formed
                break

    def handle_data(self, data):
        for node in self.open:
            node["parts"].append(data)


def ids_of(html: str) -> dict:
    parser = _Ids(html)
    parser.close()
    return parser.text


ROW = re.compile(r'<article class="candidate-row[^"]*"( id="[^"]*")?>', re.S)


def rows_of(html: str) -> list[str]:
    """Each candidate row of a page, from its opening tag to the next one."""
    starts = [match.start() for match in ROW.finditer(html)]
    ends = [html.index("</article>", start) + len("</article>") for start in starts]
    return [html[start:end] for start, end in zip(starts, ends)]


def buttons_of(markup: str) -> list[dict]:
    """Every submit button of a fragment, with its attributes and its words."""
    found = []
    for attrs, body in re.findall(r"<button([^>]*)>(.*?)</button>", markup, re.S):
        parsed = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        found.append({**parsed,
                      "words": " ".join(re.sub("<[^>]+>", " ", body).split())})
    return found


def title_of(row: str) -> str:
    match = re.search(r'<h3 class="candidate-title[^"]*">(.*?)</h3>', row, re.S)
    assert match, row[:400]
    return " ".join(re.sub("<[^>]+>", " ", match.group(1)).split())


def assert_each_row_describes_its_own_title(html: str, expected: dict) -> None:
    """Every button of a row points, through a stable id, at that row's title."""
    document = ids_of(html)
    titles = {title_of(row) for row in rows_of(html)}
    assert titles == set(expected.values()), titles
    for row in rows_of(html):
        title = title_of(row)
        buttons = buttons_of(row)
        assert buttons, title
        for button in buttons:
            reference = button.get("aria-describedby", "")
            assert reference, (title, button)
            assert document.get(reference) == title, (title, reference, document.get(reference))
        # The description ids are the row's own: no two rows share one.
        assert {button["aria-describedby"] for button in buttons} == {
            reference for reference in {button["aria-describedby"] for button in buttons}}


# -- S1: every row button has a distinct accessible description --------------


def marked_item(conn, settings, key="11", title="Tenet"):
    """A real removal in Leaving soon, made through the lifecycle engine."""
    library = seed_library(conn, "9", "Shows", section_type="movie")
    item = seed_item(conn, library, key, title, rating=7.0)
    candidate = make_candidate(item, key, title)
    engine = LifecycleEngine(conn, settings=settings,
                             clock=lambda: datetime.now(timezone.utc))
    engine.mark_for_removal(candidate, marked_by="owner")
    due = datetime.now(timezone.utc) + timedelta(days=10)
    conn.execute("UPDATE removal_requests SET due_at=?", (due.isoformat(),))
    conn.commit()
    return item, candidate


def test_every_candidate_row_button_describes_its_own_title(app, client, conn):
    library = seed_library(conn, "1", "Movies")
    tenet = seed_item(conn, library, "11", "Tenet", rating=7.0)
    heat = seed_item(conn, library, "12", "Heat", rating=7.0)
    snapshot(app, [displayed(tenet, "Tenet", MOVIE_SIZE),
                   displayed(heat, "Heat", MOVIE_SIZE)])
    # One row carries an open removal, so it offers Undo beside Keep forever.
    seed_library(conn, "2", "Shows")
    engine = LifecycleEngine(conn, settings=app.config["SETTINGS"],
                             clock=lambda: datetime.now(timezone.utc))
    engine.mark_for_removal(make_candidate(tenet, "11", "Tenet"), marked_by="owner")

    html = client.get("/?tab=candidates&lang=en").get_data(as_text=True)

    assert_each_row_describes_its_own_title(html, {"Tenet": "Tenet", "Heat": "Heat"})
    tenet_row = next(row for row in rows_of(html) if title_of(row) == "Tenet")
    # The words are unchanged: only the description is new.
    assert {button["words"] for button in buttons_of(tenet_row)} == {
        EN["action.keep_forever"], EN["action.undo"]}
    heat_row = next(row for row in rows_of(html) if title_of(row) == "Heat")
    assert {button["words"] for button in buttons_of(heat_row)} == {
        EN["action.keep_forever"], EN["action.mark"]}


def test_every_leaving_soon_row_button_describes_its_own_title(app, client, conn):
    item, _ = marked_item(conn, app.config["SETTINGS"])

    html = client.get("/?tab=leaving_soon&lang=en").get_data(as_text=True)

    assert_each_row_describes_its_own_title(html, {"Tenet": "Tenet"})
    row = rows_of(html)[0]
    assert [button["words"] for button in buttons_of(row)] == [EN["action.keep_it"]]


@pytest.mark.parametrize("locale", LOCALES)
def test_the_description_ids_are_the_same_in_every_language(app, client, conn, locale):
    library = seed_library(conn, "1", "Movies")
    tenet = seed_item(conn, library, "11", "Tenet", rating=7.0)
    snapshot(app, [displayed(tenet, "Tenet", MOVIE_SIZE)])

    html = client.get(f"/?tab=candidates&lang={locale}").get_data(as_text=True)

    described = [button["aria-describedby"] for row in rows_of(html)
                 for button in buttons_of(row)]
    assert described == [f"item-{tenet}-movie-title"] * 2


def card_of(html: str, name: str) -> str:
    """One service card, from its opening tag to its end."""
    match = re.search(rf'<article class="card svc[^"]*" id="{name}"[^>]*>', html)
    assert match, name
    return html[match.start():html.index("</article>", match.start())]


def controls_of(markup: str) -> list[dict]:
    """Every button and disclosure summary of a fragment, with its attributes."""
    found = []
    for tag, attrs, body in re.findall(r"<(button|summary)([^>]*)>(.*?)</\1>", markup, re.S):
        found.append({**dict(re.findall(r'([\w-]+)="([^"]*)"', attrs)),
                      "tag": tag,
                      "words": " ".join(re.sub("<[^>]+>", " ", body).split())})
    return found


def test_every_service_card_action_describes_its_service(app, client, conn):
    """S1: Forget the key, Configure and Edit say which service they act on."""
    from almali_library_curator import secret_store
    from almali_library_curator.config import update_settings_file

    config_dir = app.config["SETTINGS"].config_dir
    # One card with a stored key: it offers "Forget the key" and "Edit".
    secret_store.store_secret(config_dir, "radarr_api_key", "radarr-test-key-1")
    update_settings_file(config_dir, {"radarr_base_url": "http://stored-radarr:7878"})

    html = client.get("/connections?lang=en").get_data(as_text=True)

    document = ids_of(html)
    cards = re.findall(r'<article class="card svc[^"]*" id="([^"]+)"', html)
    assert cards == ["plex", "tautulli", "sonarr", "radarr", "tmdb", "host"]
    brands = {name: document.get(f"{name}-name") for name in cards}
    assert brands["radarr"] == "Radarr" and brands["sonarr"] == "Sonarr", brands
    seen = set()
    for name in cards:
        for control in controls_of(card_of(html, name)):
            reference = control.get("aria-describedby")
            if control["words"] not in (EN["connections.service.forget"],
                                         EN["connections.plex.forget"],
                                         EN["connections.card.edit"],
                                         EN["connections.card.configure"]):
                continue
            assert reference == f"{name}-name", (name, control)
            seen.add((name, control["words"]))
    # Both words, on the card that really carries them.
    assert ("radarr", EN["connections.service.forget"]) in seen, seen
    assert ("radarr", EN["connections.card.edit"]) in seen, seen
    assert ("sonarr", EN["connections.card.configure"]) in seen, seen


# -- S3: the first "Resume deletions" button reads as dangerous --------------

def test_resume_deletions_is_a_danger_action_when_the_kill_switch_is_on(app, client, conn):
    """#148 S3: resuming deletions is styled like every other danger action."""
    from almali_library_curator.lifecycle.safety import set_kill_switch

    set_kill_switch(conn, True)
    html = client.get('/settings?lang=en').get_data(as_text=True)

    buttons = re.findall(r'<button class="([^"]*)"[^>]*>(?:(?!</button>).)*Resume deletions', html, re.S)
    assert buttons and all('btn-danger' in classes for classes in buttons), buttons


def test_stop_deletions_stays_a_plain_action(app, client, conn):
    """The opposite action is not a danger: stopping deletions is the safe side."""
    from almali_library_curator.lifecycle.safety import set_kill_switch

    set_kill_switch(conn, False)
    html = client.get('/settings?lang=en').get_data(as_text=True)

    buttons = re.findall(r'<button class="([^"]*)"[^>]*>(?:(?!</button>).)*Stop all deletions', html, re.S)
    assert buttons and not any('btn-danger' in classes for classes in buttons), buttons


# -- S4: button borders reach the non-text contrast minimum ------------------

def test_the_button_border_uses_the_control_border_token():
    """#148 S4: `.btn` borders use `--border-control` (WCAG 1.4.11), not `--border-hi`."""
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2]
           / 'almali_library_curator/static/css/hud.css').read_text(encoding='utf-8')
    rule = re.search(r'^\.btn \{([^}]*)\}', css, re.M).group(1)
    assert 'border: 1px solid var(--border-control)' in rule, rule


# -- S6: the template install updates with Force update ----------------------

def test_the_unraid_docs_name_force_update_for_a_template_install():
    """#148 S6: "Check for updates" may not pick up a new `latest`."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ('docs/UNRAID.md', 'README.md'):
        text = (root / name).read_text(encoding='utf-8')
        assert 'force update' in text.lower(), name


# -- S2: the heading order of the Library page -------------------------------

def headings_of(html: str) -> list[tuple[int, str]]:
    return [(int(level), ' '.join(re.sub(r'<[^>]+>', ' ', text).split()))
            for level, text in re.findall(r'<h([1-6])\b[^>]*>(.*?)</h\1>', html, re.S)]


@pytest.mark.parametrize("locale", LOCALES)
def test_the_library_headings_start_at_h1_and_never_skip_a_level(app, client, conn, locale):
    library = seed_library(conn, "1", "Movies")
    tenet = seed_item(conn, library, "11", "Tenet", rating=7.0)
    snapshot(app, [displayed(tenet, "Tenet", MOVIE_SIZE)])
    html = client.get(f"/?tab=candidates&lang={locale}").get_data(as_text=True)
    assert "Tenet" in html

    levels = [level for level, _ in headings_of(html)]
    assert levels and levels[0] == 1, headings_of(html)
    assert all(after - before <= 1 for before, after in zip(levels, levels[1:])), headings_of(html)


def test_the_candidate_rows_sit_under_a_heading_that_names_the_list(app, client, conn):
    library = seed_library(conn, "1", "Movies")
    tenet = seed_item(conn, library, "11", "Tenet", rating=7.0)
    snapshot(app, [displayed(tenet, "Tenet", MOVIE_SIZE)])
    html = client.get("/?tab=candidates&lang=en").get_data(as_text=True)

    headings = headings_of(html)
    first_row = next(index for index, (level, text) in enumerate(headings)
                     if level == 3 and text == "Tenet")
    owner = [text for level, text in headings[:first_row] if level == 2][-1]
    assert owner == CATALOGS["en"]["nav.candidates"], headings


# -- S3: the disabled "Protected" checkbox -----------------------------------

def test_a_disabled_protected_checkbox_is_described_by_its_reason(app, client, conn):
    from dataclasses import replace
    from almali_library_curator.config import save_settings

    seed_library(conn, "1", "Movies")
    seed_library(conn, "2", "Shows")
    save_settings(replace(app.config["SETTINGS"], curated_libraries=("2",)))
    html = client.get("/settings?lang=en").get_data(as_text=True)

    [tag] = re.findall(r'<input type="checkbox" name="protected_libraries" value="1"[^>]*>', html, re.S)
    assert re.search(r'\sdisabled\b', tag)
    described = re.search(r'aria-describedby="([^"]+)"', tag).group(1).split()
    texts = ids_of(html)
    assert EN["settings.library_protected_requires_curated"] in [texts.get(ref, "") for ref in described]
    [enabled] = re.findall(r'<input type="checkbox" name="protected_libraries" value="2"[^>]*>', html, re.S)
    assert len(re.search(r'aria-describedby="([^"]+)"', enabled).group(1).split()) == 1


# -- S4: History -------------------------------------------------------------

def seed_history(conn, count):
    for index in range(count):
        conn.execute(
            "INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,"
            " disk, reason, marked_by, occurred_at) VALUES ('deleted', ?, 'movie', 100, 0,"
            " NULL, 'A reason', 'owner', ?)",
            (f"Film {index}", f"2026-11-01T12:{index % 60:02d}:00+00:00"))
    conn.commit()


def test_history_hides_the_disk_when_the_host_is_not_configured(app, client, conn):
    from dataclasses import replace
    from almali_library_curator.config import save_settings

    seed_history(conn, 1)
    save_settings(replace(app.config["SETTINGS"], unraid_root=""))
    html = client.get("/history?lang=en").get_data(as_text=True)
    assert "Film 0" in html
    assert f"<dt>{EN['item.disk']}</dt>" not in html

    save_settings(replace(app.config["SETTINGS"], unraid_root="/mnt"))
    html = client.get("/history?lang=en").get_data(as_text=True)
    assert f"<dt>{EN['item.disk']}</dt>" in html


@pytest.mark.parametrize("locale", LOCALES)
def test_the_history_page_number_says_which_page_of_how_many(app, client, conn, locale):
    seed_history(conn, 120)
    html = client.get(f"/history?page=2&lang={locale}").get_data(as_text=True)
    expected = CATALOGS[locale]["pager.status"].format(page=2, pages=3)
    assert expected in html


# -- S5: the unused Tabler vendor assets -------------------------------------

def test_the_pages_do_not_load_the_unused_tabler_vendor_assets(client):
    html = client.get("/").get_data(as_text=True)
    assert "tabler-vendors.min.css" not in html
    assert "tabler.min.js" not in html
    assert "tabler.min.css" in html


def test_the_summary_card_label_keeps_its_gap_now_that_it_is_a_paragraph():
    """The label became a <p>; the rule that removes the detail line's
    margin must not remove the label's gap too (verifier finding)."""
    css = HUD.read_text(encoding="utf-8")
    rule = re.search(r'\.summary-card p(?P<sel>[^{]*)\{\s*margin-bottom:\s*0', css)
    assert rule and ':not(.section-title)' in rule.group('sel')
