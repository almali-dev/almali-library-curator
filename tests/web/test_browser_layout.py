"""T10 gap (c): 320 px and 44 x 44 px, measured in a real headless browser.

S7: "Works from 320 px wide with no horizontal page scroll; touch targets at
least 44 x 44 px." Every screen is served with seeded demo data and opened in
Chromium at 320 px wide, in English, Catalan and Spanish (the longest strings,
L108 decision 4). The page must not scroll sideways, and every visible link,
button, tab, filter, checkbox, radio, select, input and locale switch must
measure at least 44 x 44 px. The one exemption is a link inside running text
(WCAG 2.5.8's inline exception, L108 decision 2). A checkbox or radio may
meet the rule through its clickable label.

Playwright and its Chromium come from the system Python (L108 decision 1):
when either is missing the module skips; it adds no dependency.
"""
from dataclasses import replace
import json
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from werkzeug.serving import make_server  # noqa: E402

from almali_library_curator.app import create_app  # noqa: E402
from almali_library_curator.config import Settings, save_settings  # noqa: E402
from almali_library_curator.db import connect  # noqa: E402
from demo_data import configure_arrs, seed_demo  # noqa: E402

# This module's server is module-scoped, so it sets its own login (S17).
BROWSER_USER = "owner"
BROWSER_PASSWORD = "browser-test password 51d2"
WIDTH = 320
TARGET = 44
LOCALES = ("en", "ca", "es")
SCREENS = {
    "candidates": "/",
    "leaving_soon": "/?tab=leaving_soon",
    "protected": "/?tab=protected",
    "history": "/history",
    "connections": "/connections",
    "settings": "/settings",
    "about": "/about",
    "setup_plex": "/setup?step=plex",
    "setup_libraries": "/setup?step=libraries",
    "setup_connections": "/setup?step=connections",
    "not_found": "/no-such-screen",
    "login": "/login",
}

# Measured in the page: overflow and every undersized interactive control.
MEASURE = """
() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 0 && box.height > 0;
  };
  const letters = /[\\p{L}]/u;
  // A link inside running text: inline, and its block has other words.
  const inline = (el) => {
    if (el.tagName !== 'A' || getComputedStyle(el).display !== 'inline') return false;
    if (el.closest('nav, .locale-switcher, .row-actions, .wizard-nav')) return false;
    const block = el.parentElement;
    const rest = Array.from(block.childNodes).filter((node) => node !== el)
      .map((node) => node.textContent).join('');
    return letters.test(rest);
  };
  const controls = document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=tab]');
  const small = [];
  for (const el of controls) {
    if (!visible(el) || inline(el)) continue;
    let target = el;
    if (el.matches('input[type=checkbox], input[type=radio]') && el.closest('label')) {
      target = el.closest('label');
    }
    const box = target.getBoundingClientRect();
    // Half a CSS pixel absorbs subpixel layout rounding (43.99 is 44).
    if (box.width < %(target)d - 0.5 || box.height < %(target)d - 0.5) {
      small.push(`${target.tagName.toLowerCase()}.${target.className || '-'} "${
        (target.textContent || target.name || '').trim().replace(/\\s+/g, ' ').slice(0, 40)
      }" ${Math.round(box.width)}x${Math.round(box.height)}`);
    }
  }
  return {scrollWidth: document.documentElement.scrollWidth, small};
}
""" % {"target": TARGET}


@pytest.fixture(scope="module")
def chromium():
    try:
        manager = sync_api.sync_playwright().start()
    except Exception as error:  # pragma: no cover - depends on the host
        pytest.skip(f"Playwright cannot start: {type(error).__name__}")
    try:
        browser = manager.chromium.launch()
    except Exception as error:  # pragma: no cover - depends on the host
        manager.stop()
        pytest.skip(f"Chromium cannot launch: {type(error).__name__}")
    yield browser
    browser.close()
    manager.stop()


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    """The real app with demo data, served over HTTP on a free local port."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        configure_arrs(monkeypatch)
        monkeypatch.setenv("ALC_AUTH_USER", BROWSER_USER)
        monkeypatch.setenv("ALC_AUTH_PASSWORD", BROWSER_PASSWORD)
        settings = replace(Settings(config_dir=tmp_path_factory.mktemp("browser") / "config"),
                           scheduler_enabled=False)
        save_settings(settings)
        app = create_app(settings)
        conn = connect(settings.database_path)
        try:
            seed_demo(app, conn)
            # The deletions are stopped: the screen that asks before resuming
            # them is one click away (#20).
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('kill_switch', 'on')"
                         " ON CONFLICT(key) DO UPDATE SET value='on'")
            conn.commit()
        finally:
            conn.close()
        # A waiting Plex sign-in: the panel with the polled status line is
        # measured too, on the two screens that show it (#16).
        (settings.config_dir / "plex-signin.json").write_text(json.dumps({
            "version": 1, "state": "pending", "pin_id": 7, "code": "ABCD",
            "client_identifier": "client", "message": None}))
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture(scope="module")
def narrow(chromium):
    context = chromium.new_context(viewport={"width": WIDTH, "height": 640},
                                   device_scale_factor=1, reduced_motion="reduce")
    yield context.new_page()
    context.close()


@pytest.fixture(scope="module")
def signed_in(demo_server, narrow):
    """The owner signs in once through the real login form."""
    narrow.goto(f"{demo_server}/login")
    narrow.fill("#login-user", BROWSER_USER)
    narrow.fill("#login-password", BROWSER_PASSWORD)
    with narrow.expect_navigation():
        narrow.click("form[action^='/login'] button[type=submit]")
    assert narrow.url.rstrip("/") == demo_server
    return narrow


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_every_screen_fits_320px_with_44px_targets(demo_server, signed_in, screen, locale):
    narrow = signed_in
    path = SCREENS[screen]
    response = narrow.goto(f"{demo_server}{path}{'&' if '?' in path else '?'}lang={locale}")
    assert response.status == (404 if screen == "not_found" else 200)
    narrow.evaluate("document.fonts.ready")
    assert narrow.evaluate("document.documentElement.lang") == locale
    measured = narrow.evaluate(MEASURE)
    assert measured["scrollWidth"] <= WIDTH, measured
    assert measured["small"] == []


def test_the_measure_flags_a_small_control_and_exempts_an_inline_link(narrow):
    # Leave the app document's CSP behind so the calibration styles can apply.
    narrow.goto("about:blank")
    narrow.set_content(
        '<p>Read the <a href="/a">guide</a> first.</p>'
        '<button style="width:30px;height:30px;padding:0">x</button>'
        '<label style="display:block;min-height:44px">'
        '<input type="checkbox" /> A long enough clickable label</label>'
        '<div style="width:400px">wide</div>')
    measured = narrow.evaluate(MEASURE)
    assert measured["scrollWidth"] > WIDTH
    assert measured["small"] == ['button.- "x" 30x30']


@pytest.mark.parametrize("locale", LOCALES)
def test_the_confirmation_screen_fits_320px_with_44px_targets(demo_server, signed_in, locale):
    """#20: the confirmation screen is a screen of the app too."""
    narrow = signed_in
    narrow.goto(f"{demo_server}/settings?lang={locale}")
    # The deletions are stopped: resuming them asks for a confirmation.
    with narrow.expect_navigation():
        narrow.click("form[action^='/settings/kill-switch?'] button[type=submit]")
    narrow.wait_for_selector("#confirm-heading")
    measured = narrow.evaluate(MEASURE)
    assert measured["scrollWidth"] <= WIDTH, measured
    assert measured["small"] == []
