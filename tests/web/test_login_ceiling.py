"""S3 (#145): IPv6 clients group by /64 and a whole window has a ceiling.

Behind a reverse proxy an attacker rotates addresses, so five failures per
address stopped being five guesses: an IPv6 client is now one client per /64,
and once the failures of the whole window reach the ceiling every attempt is
slowed with HTTP 429, whichever address it comes from, until the window
drains. Nothing sleeps on a request thread, and the owner is never locked out
beyond the window.

Everything goes through the app's own login form; the clock is injected, so no
test sleeps.
"""
from __future__ import annotations

from dataclasses import replace
from html import unescape
import logging
import os
import re

from almali_library_curator import auth
from almali_library_curator.app import create_app
from almali_library_curator.i18n import translate

FIFTEEN_MINUTES = 15 * 60


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def build(settings, clock):
    app = create_app(replace(settings, scheduler_enabled=False), clock=clock)
    app.config.update(TESTING=True)  # CSRF stays on, as in production
    return app


def csrf(client):
    page = client.get('/login').get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def attempt(client, address, password=None):
    data = {'user': os.environ['ALC_AUTH_USER'],
            'password': os.environ['ALC_AUTH_PASSWORD'] if password is None else password,
            'csrf_token': csrf(client)}
    return client.post('/login', data=data, environ_base={'REMOTE_ADDR': address})


def fail_from(client, addresses):
    for address in addresses:
        assert attempt(client, address, password='wrong').status_code == 401


def text(response):
    stripped = re.sub(r'<[^>]+>', ' ', response.get_data(as_text=True))
    return ' '.join(unescape(stripped).split())


def locked_message(locale='en'):
    return ' '.join(translate('login.locked', locale).split())


# -- rule 1: one client per IPv6 /64 -------------------------------------------


def test_five_failures_in_one_ipv6_64_lock_the_whole_subnet(settings):
    clock = Clock()
    client = build(settings, clock).test_client()
    subnet = '2001:db8:0:1::'
    fail_from(client, [f'{subnet}{index}' for index in range(1, 6)])

    # Another address of the same /64 is the same client: the right password
    # is not checked and the answer is the lockout page.
    response = attempt(client, f'{subnet}99')

    assert response.status_code == 429
    assert locked_message() in text(response)
    # Another /64 is another client and still signs in.
    assert attempt(client, '2001:db8:0:2::1').status_code == 303


def test_ipv4_clients_keep_counting_per_address(settings):
    clock = Clock()
    client = build(settings, clock).test_client()
    fail_from(client, ['198.51.100.1'] * 5)

    assert attempt(client, '198.51.100.1').status_code == 429
    # The recorded behaviour: an IPv4 neighbour is a different client.
    assert attempt(client, '198.51.100.2').status_code == 303


def test_an_ipv4_mapped_address_is_the_same_client_as_the_plain_one(settings):
    """A proxy can report ``::ffff:198.51.100.7`` for an IPv4 client."""
    clock = Clock()
    client = build(settings, clock).test_client()
    fail_from(client, ['::ffff:198.51.100.7'] * 5)

    assert attempt(client, '198.51.100.7').status_code == 429
    assert attempt(client, '198.51.100.8').status_code == 303


def test_a_success_in_the_64_clears_the_failures_of_the_whole_group(settings):
    clock = Clock()
    client = build(settings, clock).test_client()
    subnet = '2001:db8:0:9::'
    fail_from(client, [f'{subnet}{index}' for index in range(1, 5)])

    assert attempt(client, f'{subnet}5').status_code == 303
    fail_from(client, [f'{subnet}{index}' for index in range(1, 5)])
    assert attempt(client, f'{subnet}5').status_code == 303


def test_the_lockout_page_is_translated_for_a_grouped_client(settings):
    clock = Clock()
    client = build(settings, clock).test_client()
    fail_from(client, [f'2001:db8:1:1::{index}' for index in range(1, 6)])

    for locale in ('en', 'ca', 'es'):
        page = client.get(f'/login?lang={locale}').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = client.post(f'/login?lang={locale}', data={
            'user': os.environ['ALC_AUTH_USER'],
            'password': os.environ['ALC_AUTH_PASSWORD'],
            'csrf_token': token}, environ_base={'REMOTE_ADDR': '2001:db8:1:1::aa'})
        assert response.status_code == 429
        assert locked_message(locale) in text(response)


# -- rule 2: a process-wide ceiling on the failures of one window --------------


def fill_window(app, count):
    """``count`` failures, one from each of ``count`` different addresses."""
    fail_from(app.test_client(),
              [f'198.51.100.{index}' for index in range(1, count + 1)])


def test_the_ceiling_is_a_hundred_failures_per_window():
    assert auth.GLOBAL_MAX_FAILURES == 100


def test_a_full_window_slows_every_attempt_whichever_address_it_comes_from(settings, caplog):
    clock = Clock()
    app = build(settings, clock)
    caplog.set_level(logging.WARNING)

    # Under the ceiling the owner can still sign in, even after 99 failures.
    fill_window(app, auth.GLOBAL_MAX_FAILURES - 1)
    assert attempt(app.test_client(), '203.0.113.9').status_code == 303
    # The ceiling counts the whole window, not one address: one more failure
    # fills it, and no address carries more than one.
    fail_from(app.test_client(), ['198.51.100.200'])

    response = attempt(app.test_client(), '203.0.113.10')

    assert response.status_code == 429
    assert locked_message() in text(response)
    # The refusal happened once, not once per request of the window.
    events = [record for record in caplog.records
              if record.getMessage() == 'login_guard_ceiling_reached']
    assert len(events) == 1


def test_the_ceiling_drains_with_the_window_so_the_owner_gets_back_in(settings):
    clock = Clock()
    app = build(settings, clock)
    fill_window(app, auth.GLOBAL_MAX_FAILURES)
    assert attempt(app.test_client(), '203.0.113.11').status_code == 429

    # The window is fifteen minutes, like the per-address one: one second
    # short of it the ceiling still answers, after it the sign-in works.
    clock.now += FIFTEEN_MINUTES - 1
    assert attempt(app.test_client(), '203.0.113.11').status_code == 429
    clock.now += 1
    assert attempt(app.test_client(), '203.0.113.11').status_code == 303


def test_every_address_under_the_ceiling_keeps_its_own_limit(settings):
    """The ceiling adds to the per-address limit; it does not replace it."""
    clock = Clock()
    client = build(settings, clock).test_client()
    fail_from(client, ['192.0.2.20'] * 5)

    assert attempt(client, '192.0.2.20').status_code == 429
    assert attempt(client, '192.0.2.21').status_code == 303
