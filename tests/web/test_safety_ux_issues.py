"""Regression coverage for the v0.24.0 safety UX fixes (issues #16, #18–20, #23).

One test per rule, each of them observable through the public interface (the
HTTP routes an owner uses), plus a test that the behaviour next to the change
still holds: stopping the deletions, the other removal actions and the error
page keep working exactly as before.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re

import pytest
from flask.testing import FlaskClient
from markupsafe import Markup

from almali_library_curator import secret_store
from almali_library_curator.config import load_settings, save_settings
from almali_library_curator.lifecycle.safety import kill_switch_enabled, set_kill_switch
from almali_library_curator.web import actions
from demo_data import seed_demo


ROOT = Path(__file__).resolve().parents[2] / "almali_library_curator"
CATALOGS = {
    locale: json.loads((ROOT / f"i18n/{locale}.json").read_text())
    for locale in ("en", "ca", "es")
}
TOKEN = "stored-plex-token-7f3c9ab12"
PENDING_SIGNIN = {
    "version": 1,
    "state": "pending",
    "pin_id": 7,
    "code": "ABCD",
    "client_identifier": "client",
    "message": None,
}


def text(response):
    return " ".join(Markup(response.get_data(as_text=True)).striptags().split())


def write_signin(app, state, **extra):
    """The sign-in state file the app writes while a sign-in waits."""
    path = app.config["SETTINGS"].config_dir / actions.SIGNIN_FILE_NAME
    path.write_text(json.dumps({**PENDING_SIGNIN, "state": state, **extra}))
    return path


def badge(html, state):
    """The markup of one state badge, or an empty string."""
    match = re.search(r'<span class="badge badge-state state-%s">(.*?)</span>' % state, html, re.S)
    return match.group(0) if match else ""


# -- #16: the pending sign-in must not reload the page -----------------------

def test_the_pending_signin_updates_its_status_without_reloading_the_page(app, client):
    """#16: a half-typed manual address or token must survive the wait."""
    write_signin(app, "pending")

    response = client.get("/connections")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    # No automatic reload of the whole page, and no inline script either.
    assert '<meta http-equiv="refresh"' not in html
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", html))
    # Only the status region is polled, and the script is the app's own file.
    assert 'id="plex-signin-status"' in html
    assert 'data-status-url="/connections/plex/signin/status' in html
    assert '/static/js/plex-signin-status.js' in html
    script = client.get("/static/js/plex-signin-status.js")
    assert script.status_code == 200 and script.data
    # The manual form is still on the page, and nothing reloads it.
    assert 'name="plex_base_url"' in html and 'name="plex_token"' in html
    assert CATALOGS["en"]["connections.plex.signin.waiting"] in text(response)


def test_the_status_endpoint_reports_the_signin_state_and_never_consumes_it(app, client):
    """#16: the poll reports the state; it does not take it away."""
    write_signin(app, "pending")

    response = client.get("/connections/plex/signin/status")
    assert response.status_code == 200
    assert response.json["state"] == "pending"
    assert response.json["pending"] is True
    assert CATALOGS["en"]["connections.plex.signin.waiting"] == response.json["message"]

    write_signin(app, "succeeded", message="connections.plex.signin.succeeded")
    first = client.get("/connections/plex/signin/status").json
    second = client.get("/connections/plex/signin/status").json
    assert first == second
    assert first["state"] == "succeeded" and first["pending"] is False


def test_the_status_endpoint_needs_the_owner_session(app, client):
    """#16: the status of the sign-in is not public information."""
    write_signin(app, "pending")
    assert client.get("/connections/plex/signin/status").status_code == 200

    anonymous = FlaskClient(app)
    response = anonymous.get("/connections/plex/signin/status")
    assert response.status_code == 303
    assert "/login" in response.headers["Location"]


# -- #18: the kill switch needs its own badges --------------------------------

@pytest.mark.parametrize("locale", ("en", "ca", "es"))
@pytest.mark.parametrize("engaged", (False, True))
def test_the_kill_switch_badge_says_which_state_it_is_in(client, conn, locale, engaged):
    """#18: a safe state must not look like a failure, and an armed one must
    not look like protection."""
    set_kill_switch(conn, engaged)

    response = client.get(f"/settings?lang={locale}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    state = "kill_switch_on" if engaged else "kill_switch_off"
    other = "kill_switch_off" if engaged else "kill_switch_on"
    assert f"state-{state}" in html and f"state-{other}" not in html
    assert CATALOGS[locale][f"state.{state}"] in text(response)
    # The old badges were a failure and a protection: neither may come back.
    assert "state-removal_failed" not in html and "state-protected" not in html
    # The badge carries an icon and words: colour is never the only cue.
    markup = badge(html, state)
    assert markup and '<use href="/static/icons.svg#' in markup
    assert CATALOGS[locale][f"state.{state}"] in " ".join(
        Markup(markup).striptags().split())


# -- #19: the guards are visible on every Library tab --------------------------

@pytest.mark.parametrize("tab", ("candidates", "leaving_soon", "protected"))
def test_every_library_tab_shows_the_dry_run_and_the_kill_switch(app, client, conn, tab):
    """#19: dry run and the kill switch belong to the whole screen, not to one
    tab."""
    seed_demo(app, conn)
    set_kill_switch(conn, True)

    response = client.get(f"/?tab={tab}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "state-dry_run" in html
    assert "state-kill_switch_on" in html
    assert CATALOGS["en"]["state.dry_run"] in text(response)
    assert CATALOGS["en"]["state.kill_switch_on"] in text(response)

    set_kill_switch(conn, False)
    response = client.get(f"/?tab={tab}")
    html = response.get_data(as_text=True)
    assert "state-kill_switch_off" in html
    assert CATALOGS["en"]["state.kill_switch_off"] in text(response)


def test_the_recheck_line_never_promises_a_deletion_while_a_guard_is_active(app, client, conn):
    """#19: "awaiting deletion" is only true when a deletion can happen."""
    settings = seed_demo(app, conn)
    request_id = conn.execute(
        "SELECT id FROM removal_requests WHERE state='marked'").fetchone()[0]
    conn.execute("UPDATE removal_requests SET state='recheck_passed', recheck_status='passed'"
                 " WHERE id=?", (request_id,))
    conn.commit()

    # Dry run (the default of a fresh install): nothing will be deleted.
    body = text(client.get("/?tab=leaving_soon"))
    assert CATALOGS["en"]["leaving.recheck_status.dry_run"] in body
    assert CATALOGS["en"]["leaving.recheck_status.passed"] not in body

    # Live mode with the kill switch on: the deletion is stopped.
    save_settings(replace(settings, dry_run=False))
    set_kill_switch(conn, True)
    body = text(client.get("/?tab=leaving_soon"))
    assert CATALOGS["en"]["leaving.recheck_status.kill_switch"] in body
    assert CATALOGS["en"]["leaving.recheck_status.passed"] not in body

    # Live mode with the kill switch off: the deletion is really pending.
    set_kill_switch(conn, False)
    body = text(client.get("/?tab=leaving_soon"))
    assert CATALOGS["en"]["leaving.recheck_status.passed"] in body


# -- #20: the two actions that arm deletions again ask first -------------------

def test_resuming_deletions_asks_first_and_waits_for_the_owner(client, conn):
    """#20: one click may stop the deletions, not start them again."""
    # Stopping the deletions is the safe direction: it needs no confirmation.
    assert client.post("/settings/kill-switch", data={"enabled": "on"}).status_code == 303
    assert kill_switch_enabled(conn) is True

    confirmation = client.post("/settings/kill-switch", data={"enabled": "off"})
    html = confirmation.get_data(as_text=True)

    assert confirmation.status_code == 200
    assert kill_switch_enabled(conn) is True
    assert CATALOGS["en"]["action.resume_deletions"] in text(confirmation)
    assert 'action="/settings/kill-switch/confirm' in html
    assert 'name="csrf_token"' in html
    # Leaving the confirmation changes nothing.
    assert client.get("/settings").status_code == 200
    assert kill_switch_enabled(conn) is True

    resumed = client.post("/settings/kill-switch/confirm")
    assert resumed.status_code == 303
    assert kill_switch_enabled(conn) is False


def test_forgetting_the_plex_token_asks_first_and_keeps_the_token(app, client):
    """#20: the stored token survives until the owner confirms."""
    config_dir = app.config["SETTINGS"].config_dir
    secret_store.store_plex_token(config_dir, TOKEN)

    confirmation = client.post("/connections/plex/forget", data={"origin": "connections"})
    html = confirmation.get_data(as_text=True)

    assert confirmation.status_code == 200
    assert load_settings(config_dir, env={}).plex_token == TOKEN
    assert CATALOGS["en"]["connections.plex.forget"] in text(confirmation)
    assert 'action="/connections/plex/forget/confirm' in html
    assert 'name="csrf_token"' in html
    # Leaving the confirmation changes nothing.
    assert client.get("/connections").status_code == 200
    assert load_settings(config_dir, env={}).plex_token == TOKEN

    forgotten = client.post("/connections/plex/forget/confirm", data={"origin": "connections"})
    assert forgotten.status_code == 303
    assert load_settings(config_dir, env={}).plex_token == ""


def test_the_other_removal_actions_still_act_without_a_confirmation(app, client, conn):
    """#20: undo and keep were never the dangerous ones."""
    seed_demo(app, conn)
    request_id = conn.execute(
        "SELECT id FROM removal_requests WHERE state='marked'").fetchone()[0]

    undone = client.post(f"/requests/{request_id}/undo")
    assert undone.status_code == 303
    state = conn.execute("SELECT state FROM removal_requests WHERE id=?",
                         (request_id,)).fetchone()[0]
    assert state == "cancelled"


# -- #23: the error page offers no navigation ----------------------------------

def test_an_error_page_has_no_navigation_to_other_screens(client):
    """#23: every link of the sidebar would answer the same error."""
    response = client.get("/no-such-screen")
    html = response.get_data(as_text=True)

    assert response.status_code == 404
    assert "data-nav=" not in html
    # The page itself is still a page of the app.
    assert 'class="page-footer"' in html
    assert CATALOGS["en"]["error.back"] in text(response)
