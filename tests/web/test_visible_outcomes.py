"""Visible outcomes after an action, a scan and a slow save (S1, S4, S6, #147).

Every assertion goes through the public interface: the HTTP routes an owner
presses, the markup the server answers with, the app's own CSS and the app's
own scripts. The scripts are asserted as the files the browser loads, because
their effect (a sticky region that stays in view, a disabled submit button) is
only observable in a browser; the browser-measured half lives in
``tests/web/test_browser_layout.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
CATALOGS = {locale: json.loads((ROOT / f"i18n/{locale}.json").read_text())
            for locale in ("en", "ca", "es")}
EN = CATALOGS["en"]


def text(response) -> str:
    return " ".join(Markup(response.get_data(as_text=True)).striptags().split())


def inside_main(html: str) -> str:
    """The part of the page between <main> and </main>."""
    return html.split('<main id="main-content"', 1)[1].split("</main>", 1)[0]


def form_tag(html: str, action: str) -> str:
    """The opening tag of the form posting to ``action`` (query included)."""
    found = re.search(r'<form[^>]*action="' + re.escape(action) + r'(?:\?[^"]*)?"[^>]*>', html)
    return found.group(0) if found else ""


def css_rule(css: str, selector: str) -> str:
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return found.group(1) if found else ""


# -- S1: the row-action outcome is visible where the page lands ---------------


@pytest.mark.parametrize("locale", ("en", "ca", "es"))
def test_a_row_action_returns_to_its_row_with_a_flash_inside_main(client, locale):
    response = client.post(f"/items/999/mark?lang={locale}",
                           data={"unit": "movie", "tab": "candidates",
                                 "anchor": "item-1-movie"})

    assert response.status_code == 303
    assert response.headers["Location"].endswith("#item-1-movie")
    page = client.get(response.headers["Location"])
    main = inside_main(page.get_data(as_text=True))
    assert '<div class="flash-region">' in main
    # The outcome is still the same flash: assertive for a refusal.
    assert re.search(r'<div class="flash-message" role="alert">', main)
    assert CATALOGS[locale]["web.flash.invalid_scope"] in text(page)
    # It sits above the screen's own content, so it is read first.
    assert main.index("flash-region") < main.index("library-order")


def test_the_flash_region_is_sticky_at_the_top_of_the_viewport():
    css = (ROOT / "static" / "css" / "hud.css").read_text()
    rule = css_rule(css, ".flash-region")

    assert "position: sticky" in rule, rule
    assert re.search(r"top:\s*0", rule), rule


def test_a_successful_action_keeps_its_polite_role_inside_the_region(client):
    response = client.post("/settings?lang=en", data={"grace_days": "31"})

    assert response.status_code == 303
    main = inside_main(client.get(response.headers["Location"]).get_data(as_text=True))
    assert '<div class="flash-region">' in main
    assert re.search(r'<div class="flash-message" role="status">', main)
    assert EN["web.flash.settings_saved"] in " ".join(Markup(main).striptags().split())


def test_the_flash_carries_a_close_button_only_when_javascript_runs(client):
    followed = client.post("/settings?lang=en", data={"grace_days": "31"})
    assert followed.status_code == 303
    page = client.get(followed.headers["Location"])
    html = page.get_data(as_text=True)
    button = re.search(r'<button[^>]*data-dismiss-flash[^>]*>', html)

    # The button is part of the page, with the translated name of its action.
    assert button, "a flash carries a close button"
    assert f'aria-label="{CATALOGS["en"]["flash.dismiss"]}"' in button.group(0)

    css = (ROOT / "static" / "css" / "hud.css").read_text()
    assert re.search(r"\.flash-dismiss\s*\{[^}]*display:\s*none", css)
    assert re.search(r"\.js\s+\.flash-dismiss\s*\{[^}]*display:\s*(inline-)?flex", css)

    script = client.get("/static/js/outcome-notice.js")
    assert script.status_code == 200
    body = script.get_data(as_text=True)
    assert "data-dismiss-flash" in body
    # The button only exists for a reader with scripts: the class is added by
    # the script itself, so no page ships a control that does nothing.
    assert "classList.add" in body and "'js'" in body
    assert "js/outcome-notice.js" in html


# -- S4: the dry-run-off confirmation is found --------------------------------


def test_turning_dry_run_off_notices_at_the_top_and_links_to_the_panel(client):
    response = client.post("/settings?lang=en", data={"dry_run": "false"})
    html = response.get_data(as_text=True)
    page = text(response)

    assert response.status_code == 200
    notice = re.search(r'<div[^>]*id="dry-run-off-notice"[^>]*>', html)
    assert notice, "the notice is the top of the form"
    assert 'tabindex="-1"' in notice.group(0)
    assert "data-focus-on-load" in notice.group(0)
    assert 'href="#dry-run-off-heading"' in html
    assert "id=\"dry-run-off-heading\"" in html
    assert EN["settings.dry_run_off.notice"] in page
    assert EN["settings.dry_run_off.notice_link"] in page
    # At the top: inside the settings form and before its first field.
    assert html.index('class="settings-form"') < html.index('id="dry-run-off-notice"')
    assert html.index('id="dry-run-off-notice"') < html.index('id="field-locale"')
    assert html.index('id="dry-run-off-notice"') < html.index('id="dry-run-off-heading"')


def test_the_notice_is_not_shown_when_there_is_nothing_to_confirm(client):
    html = client.get("/settings?lang=en").get_data(as_text=True)

    assert 'id="dry-run-off-notice"' not in html
    # The panel itself is only rendered on the answer that asks to confirm.
    assert 'class="danger-panel' not in html


def test_the_notice_is_focused_once_by_the_app_script(client):
    script = client.get("/static/js/outcome-notice.js")

    assert script.status_code == 200
    body = script.get_data(as_text=True)
    assert "data-focus-on-load" in body
    assert re.search(r"\.focus\(\)", body)


@pytest.mark.parametrize("locale", ("en", "ca", "es"))
def test_the_notice_copy_is_translated(client, locale):
    page = text(client.post(f"/settings?lang={locale}", data={"dry_run": "false"}))

    assert CATALOGS[locale]["settings.dry_run_off.notice"] in page
    assert CATALOGS[locale]["settings.dry_run_off.notice_link"] in page


# -- S6: slow saves show they are working -------------------------------------


BUSY_FORMS = (
    ("/connections?lang=en", "/connections/plex"),
    ("/connections?lang=en", "/connections/sonarr"),
    ("/connections?lang=en", "/connections/radarr"),
    ("/connections?lang=en", "/connections/tautulli"),
    ("/connections?lang=en", "/connections/tmdb"),
    ("/setup?step=plex&lang=en", "/connections/plex"),
    ("/setup?step=connections&lang=en", "/connections/sonarr"),
)


@pytest.mark.parametrize("path,action", BUSY_FORMS)
def test_every_slow_save_is_a_busy_form(client, path, action):
    html = client.get(path).get_data(as_text=True)
    tag = form_tag(html, action)

    assert tag, (path, action)
    assert "data-busy" in tag
    assert f'data-busy-label="{EN["action.working"]}"' in tag
    # Without JavaScript nothing is busy: the state is not baked into the page.
    assert "aria-busy" not in tag and " disabled" not in tag


def test_the_scan_actions_are_busy_forms(client):
    for path in ("/settings?lang=en", "/setup?step=libraries&lang=en"):
        html = client.get(path).get_data(as_text=True)
        forms = re.findall(r'<form[^>]*action="/actions/scan[^"]*"[^>]*>', html)
        assert forms, path
        for tag in forms:
            assert "data-busy" in tag, (path, tag)
            assert f'data-busy-label="{EN["action.working"]}"' in tag, (path, tag)


def test_the_empty_candidate_list_offers_a_busy_scan(client):
    html = client.get("/?lang=en").get_data(as_text=True)
    for tag in re.findall(r'<form[^>]*action="/actions/scan[^"]*"[^>]*>', html):
        assert "data-busy" in tag and "data-busy-label" in tag


@pytest.mark.parametrize("path", ["/connections?lang=en",
                                    "/setup?step=plex&lang=en",
                                    "/setup?step=connections&lang=en"])
def test_every_connection_test_is_a_get_form_button(client, path):
    html = client.get(path).get_data(as_text=True)

    assert not re.search(r'<a[^>]*class="btn btn-primary"[^>]*href="[^"]*test=1', html)
    form = re.search(r'<form method="get"[^>]*data-busy[^>]*>(.*?)</form>', html, re.S)
    assert form, html[:400]
    # The probe is a plain GET the app already answers, and it is marked busy.
    assert 'name="test" value="1"' in form.group(1)
    assert re.search(r'<button[^>]*type="submit"', form.group(1))
    assert EN["connections.test"] in " ".join(Markup(form.group(1)).striptags().split())


def test_the_busy_script_marks_only_the_form_that_was_submitted(client):
    script = client.get("/static/js/busy-forms.js")

    assert script.status_code == 200
    body = script.get_data(as_text=True)
    assert "form[data-busy]" in body
    assert "aria-busy" in body
    assert "disabled" in body
    # The label is the page's translated one, never a literal in the script.
    assert "data-busy-label" in body
    # One form at a time: the script scopes every change to the submitted form.
    assert "addEventListener('submit'" in body or 'addEventListener("submit"' in body
    assert "document.querySelectorAll('button'" not in body


def test_the_shell_loads_the_feedback_scripts_once(client):
    html = client.get("/settings?lang=en").get_data(as_text=True)

    assert html.count("js/outcome-notice.js") == 1
    assert html.count("js/busy-forms.js") == 1
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", html))
