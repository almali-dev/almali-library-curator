"""S21 rules 1-3: the absolute session lifetime, the revocable sign-out and
the bounded brute-force guard (T16, work unit W16-A).

Everything goes through the Flask test client, the config directory and the
guard's public interface; no test reaches into the session machinery.
"""
from dataclasses import replace
from html import unescape
import logging
import os
import re
import stat
import time
from urllib.parse import urlsplit

import pytest

from almali_library_curator import auth
from almali_library_curator.app import create_app
from almali_library_curator.i18n import translate


TWELVE_HOURS = 12 * 60 * 60
FIFTEEN_MINUTES = 15 * 60
ADDRESS_LIMIT = 1024
LOCALES = ('en', 'ca', 'es')


class WallClock:
    """A wall clock the tests move by hand (seconds since the epoch)."""

    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def build(settings, **kwargs):
    app = create_app(replace(settings, scheduler_enabled=False), **kwargs)
    app.config.update(TESTING=True)  # the CSRF guard stays on, as in production
    return app


def csrf(client, path='/login'):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def page_text(response):
    """The visible text, with HTML entities and markup removed."""
    rendered = unescape(response.get_data(as_text=True))
    return ' '.join(re.sub(r'<[^>]+>', ' ', rendered).split())


def owner_login(client, password=None):
    data = {'user': os.environ['ALC_AUTH_USER'],
            'password': os.environ['ALC_AUTH_PASSWORD'] if password is None else password,
            'csrf_token': csrf(client)}
    return client.post('/login', data=data)


def sign_out(client, locale=None):
    query = f'?lang={locale}' if locale else ''
    token = csrf(client, f'/{query}')
    return client.post(f'/logout{query}', data={'csrf_token': token})


def addresses(count):
    """``count`` distinct client addresses."""
    return [f'198.51.100.{index // 256}.{index % 256}' for index in range(count)]


def drive(guard, pool, rounds):
    """``rounds`` rounds of one failed attempt per address."""
    for _ in range(rounds):
        for address in pool:
            attempt = guard.admit(address)
            if attempt is not None:
                guard.failed(address, attempt)


# -- rule 1: the absolute session lifetime ---------------------------------------


def test_a_session_ends_twelve_hours_after_the_sign_in(settings):
    clock = WallClock()
    client = build(settings, wall_clock=clock).test_client()
    assert owner_login(client).status_code == 303
    clock.now += TWELVE_HOURS - 1
    assert client.get('/').status_code == 200
    clock.now += 2
    assert client.get('/').status_code == 303


def test_an_expired_session_is_sent_to_the_login(settings):
    clock = WallClock()
    client = build(settings, wall_clock=clock).test_client()
    owner_login(client)
    clock.now += TWELVE_HOURS + 1
    response = client.get('/history')
    assert response.status_code == 303
    location = urlsplit(response.headers['Location'])
    assert location.path == '/login'


def test_an_expired_session_answers_401_json_on_the_scan(settings):
    clock = WallClock()
    client = build(settings, wall_clock=clock).test_client()
    owner_login(client)
    token = csrf(client)
    clock.now += TWELVE_HOURS + 1
    response = client.post('/scan', json={}, headers={'X-CSRFToken': token})
    assert response.status_code == 401
    assert response.is_json and response.get_json()['error']


def test_using_a_session_never_extends_it(settings):
    clock = WallClock()
    client = build(settings, wall_clock=clock).test_client()
    owner_login(client)
    clock.now += TWELVE_HOURS - 60
    assert client.get('/').status_code == 200  # the session is used
    clock.now += 120
    assert client.get('/').status_code == 303  # and it still ends on time


def test_a_session_without_the_sign_in_time_is_expired(settings):
    # A session issued before the change carries no sign-in time: it is
    # expired, however recent it looks.
    clock = WallClock()
    client = build(settings, wall_clock=clock).test_client()
    owner_login(client)
    with client.session_transaction() as session:
        session.pop(auth.SESSION_SIGNED_IN_AT, None)
    assert client.get('/').status_code == 303


def test_the_sign_in_time_holds_on_every_worker_and_after_a_restart(settings):
    clock = WallClock()
    first = build(settings, wall_clock=clock).test_client()
    owner_login(first)
    cookie = first.get_cookie('session')
    restarted = build(settings, wall_clock=clock).test_client()
    restarted.set_cookie('session', cookie.value)
    assert restarted.get('/').status_code == 200
    clock.now += TWELVE_HOURS + 1
    assert restarted.get('/').status_code == 303


# -- rule 2: the sign-out ends every session ---------------------------------------


def test_ensure_keeps_the_epoch_written_by_another_worker(tmp_path, monkeypatch):
    """The first worker to publish an epoch wins, even on the initial write."""
    epoch_file = tmp_path / auth.SESSION_EPOCH_FILE
    epoch = auth.SessionEpoch(epoch_file)
    published = 'a' * 32
    real_flock = auth.fcntl.flock

    def racing_flock(fd, operation):
        # Another worker publishes just before this worker takes the lock.
        if operation == auth.fcntl.LOCK_EX:
            epoch_file.write_text(published)
        return real_flock(fd, operation)

    monkeypatch.setattr(auth.fcntl, 'flock', racing_flock)
    assert epoch.ensure() == published
    assert epoch_file.read_text().strip() == published


def test_advance_reports_whether_the_epoch_was_persisted(tmp_path, monkeypatch):
    epoch_file = tmp_path / auth.SESSION_EPOCH_FILE
    epoch = auth.SessionEpoch(epoch_file)
    assert epoch.advance() is True
    before = epoch_file.read_text()

    def failing_replace(source, target):
        raise OSError('no space left on device')

    monkeypatch.setattr(os, 'replace', failing_replace)
    assert epoch.advance() is False
    assert epoch_file.read_text() == before


def test_a_cookie_copied_before_the_sign_out_is_refused_afterwards(settings):
    app = build(settings)
    client = app.test_client()
    owner_login(client)
    cookie = client.get_cookie('session')
    assert sign_out(client).status_code == 303
    replayed = app.test_client()
    replayed.set_cookie('session', cookie.value)
    response = replayed.get('/')
    assert response.status_code == 303
    assert urlsplit(response.headers['Location']).path == '/login'


def test_the_sign_out_ends_the_session_of_every_browser(settings):
    app = build(settings)
    first = app.test_client()
    second = app.test_client()
    owner_login(first)
    owner_login(second)
    assert second.get('/').status_code == 200
    assert sign_out(first).status_code == 303
    assert second.get('/').status_code == 303


def test_signing_in_again_works_after_a_sign_out(settings):
    client = build(settings).test_client()
    owner_login(client)
    sign_out(client)
    assert owner_login(client).status_code == 303
    assert client.get('/').status_code == 200


def test_the_sign_out_survives_a_restart(settings):
    app = build(settings)
    client = app.test_client()
    owner_login(client)
    cookie = client.get_cookie('session')
    sign_out(client)
    restarted = build(settings).test_client()
    restarted.set_cookie('session', cookie.value)
    assert restarted.get('/').status_code == 303


def test_the_epoch_file_is_owner_only_and_lives_in_the_config_dir(settings):
    client = build(settings).test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    assert epoch_file.exists()
    assert stat.S_IMODE(epoch_file.stat().st_mode) == 0o600
    assert re.fullmatch(r'[0-9a-f]{32}', epoch_file.read_text().strip())


def test_the_epoch_file_is_replaced_whole(settings, monkeypatch):
    client = build(settings).test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    before = epoch_file.read_text()
    assert sign_out(client).status_code == 303
    after = epoch_file.read_text()
    assert after != before
    assert len(after.strip()) == 32
    leftovers = [path.name for path in settings.config_dir.iterdir()
                 if path.name.startswith('.session_epoch.tmp.')]
    assert leftovers == []


@pytest.mark.parametrize('locale', LOCALES)
def test_a_failed_epoch_write_keeps_the_previous_epoch(
        settings, monkeypatch, caplog, locale):
    app = build(settings)
    client = app.test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    before = epoch_file.read_text()
    copied_cookie = client.get_cookie('session')

    def failing_replace(source, target):
        raise OSError('no space left on device')

    monkeypatch.setattr(os, 'replace', failing_replace)
    with caplog.at_level(logging.WARNING, logger='almali_library_curator.app'):
        response = sign_out(client, locale)

    assert response.status_code == 503
    body = page_text(response)
    for key in ('error.logout_revoke_failed.title',
                'error.logout_revoke_failed.message'):
        assert translate(key, locale) in body
    assert epoch_file.read_text() == before
    assert any(record.message == 'auth_logout_revoke_failed'
               for record in caplog.records)

    # This browser is signed out even though the global revocation failed.
    local = client.get('/')
    assert local.status_code == 303
    assert urlsplit(local.headers['Location']).path == '/login'

    # The epoch did not change, so a cookie copied before the failed sign-out
    # remains valid until the epoch can be advanced or the session expires.
    replayed = app.test_client()
    replayed.set_cookie('session', copied_cookie.value)
    assert replayed.get('/').status_code == 200


def test_a_missing_epoch_file_never_stops_the_app(settings):
    client = build(settings).test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    epoch_file.unlink()
    restarted = build(settings)
    assert restarted.test_client().get('/login').status_code == 200
    fresh = restarted.test_client()
    assert owner_login(fresh).status_code == 303
    assert epoch_file.exists()
    assert stat.S_IMODE(epoch_file.stat().st_mode) == 0o600


def test_a_corrupt_epoch_file_never_stops_the_app(settings):
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    epoch_file.parent.mkdir(parents=True, exist_ok=True)
    epoch_file.write_text('not an epoch')
    client = build(settings).test_client()
    assert owner_login(client).status_code == 303
    assert client.get('/').status_code == 200
    assert epoch_file.read_text().strip() != 'not an epoch'


def test_an_unreadable_epoch_file_never_stops_the_app(settings):
    client = build(settings).test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    epoch_file.chmod(0o000)
    try:
        restarted = build(settings)
        fresh = restarted.test_client()
        assert owner_login(fresh).status_code == 303
        assert fresh.get('/').status_code == 200
    finally:
        epoch_file.chmod(0o600)


def test_a_sign_out_without_the_csrf_token_ends_no_session(settings):
    client = build(settings).test_client()
    owner_login(client)
    epoch_file = settings.config_dir / auth.SESSION_EPOCH_FILE
    before = epoch_file.read_text()
    assert client.post('/logout').status_code == 400
    assert epoch_file.read_text() == before
    assert client.get('/').status_code == 200


# -- rule 3: the bounded guard -------------------------------------------------------


def test_the_guard_tracks_at_most_1024_addresses():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    for address in addresses(1100):
        attempt = guard.admit(address)
        assert attempt is not None
        guard.failed(address, attempt)
    assert guard.tracked_addresses() <= ADDRESS_LIMIT


def test_the_expired_addresses_are_cleaned_up_first():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    for address in addresses(1100):
        attempt = guard.admit(address)
        guard.failed(address, attempt)
    clock.now += FIFTEEN_MINUTES + 1
    # The cleanup runs every few attempts and drops the expired addresses.
    for index in range(auth.CLEANUP_INTERVAL):
        guard.admit(f'203.0.113.{index}')
    assert guard.tracked_addresses() < ADDRESS_LIMIT


def test_an_address_with_an_active_lock_is_never_evicted():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    locked = '198.51.100.7'
    for _ in range(5):
        attempt = guard.admit(locked)
        guard.failed(locked, attempt)
    assert guard.locked(locked)
    for address in addresses(1100):
        attempt = guard.admit(address)
        if attempt is not None:
            guard.failed(address, attempt)
    assert guard.tracked_addresses() <= ADDRESS_LIMIT
    assert guard.locked(locked)
    assert guard.admit(locked) is None


def test_the_lock_thresholds_and_durations_stay_the_same():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    address = '192.0.2.1'
    for _ in range(4):
        attempt = guard.admit(address)
        assert attempt is not None
        guard.failed(address, attempt)
    assert not guard.locked(address)
    attempt = guard.admit(address)
    guard.failed(address, attempt)
    assert guard.locked(address)
    clock.now += FIFTEEN_MINUTES - 1
    assert guard.locked(address)
    clock.now += 2
    assert not guard.locked(address)
    assert guard.admit(address) is not None


def test_a_success_clears_the_failures_of_the_address():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    address = '192.0.2.2'
    for _ in range(4):
        attempt = guard.admit(address)
        guard.failed(address, attempt)
    attempt = guard.admit(address)
    guard.succeeded(address, attempt)
    for _ in range(4):
        attempt = guard.admit(address)
        assert attempt is not None
        guard.failed(address, attempt)
    assert not guard.locked(address)


def test_the_guard_stays_fast_with_a_full_table():
    """The cleanup is amortised: a full table costs a bounded factor, not more.

    The same number of attempts over a full table may cost more than over ten
    addresses, but a bounded factor: a cleanup per attempt would make the
    full table grow with every attempt instead.
    """
    clock = WallClock()
    full = auth.LoginGuard(clock)
    small = auth.LoginGuard(clock)
    started = time.monotonic()
    drive(full, addresses(1100), 5)
    full_elapsed = time.monotonic() - started
    started = time.monotonic()
    drive(small, addresses(10), 550)
    small_elapsed = time.monotonic() - started
    assert full.tracked_addresses() <= ADDRESS_LIMIT
    assert full_elapsed < small_elapsed * 200


def test_a_saturated_guard_refuses_new_addresses_until_a_lock_expires():
    clock = WallClock()
    guard = auth.LoginGuard(clock, max_addresses=8)
    locked = addresses(7)
    for address in locked:
        drive(guard, [address], 5)

    # A known, unlocked address keeps working normally.
    known = addresses(8)[7]
    attempt = guard.admit(known)
    assert attempt is not None
    guard.failed(known, attempt)
    for _ in range(4):
        attempt = guard.admit(known)
        assert attempt is not None
        guard.failed(known, attempt)

    # Every slot now holds an active lock: an unseen address is refused.
    unseen = '203.0.113.200'
    assert guard.admit(unseen) is None

    clock.now += FIFTEEN_MINUTES + 1
    assert guard.admit(unseen) is not None


def test_a_saturated_guard_answers_429_for_a_new_address(settings, monkeypatch):
    real_guard = auth.LoginGuard

    def small_guard(clock=None, **_kwargs):
        return real_guard(clock, max_addresses=8)

    monkeypatch.setattr(auth, 'LoginGuard', small_guard)
    client = build(settings).test_client()
    token = csrf(client)
    data = {
        'user': os.environ['ALC_AUTH_USER'],
        'password': 'wrong',
        'csrf_token': token,
    }
    for address in addresses(8):
        for _ in range(5):
            response = client.post(
                '/login', data=data, environ_base={'REMOTE_ADDR': address})
            assert response.status_code == 401

    data['password'] = os.environ['ALC_AUTH_PASSWORD']
    response = client.post(
        '/login', data=data, environ_base={'REMOTE_ADDR': '203.0.113.200'})
    assert response.status_code == 429


def test_the_lockout_still_answers_429_through_the_app(settings):
    clock = WallClock()
    client = build(settings, clock=clock).test_client()
    for _ in range(5):
        assert owner_login(client, password='wrong').status_code == 401
    assert owner_login(client).status_code == 429


def test_many_active_locks_do_not_block_evicting_unlocked_addresses():
    clock = WallClock()
    guard = auth.LoginGuard(clock)
    locked = addresses(40)
    drive(guard, locked, 5)
    assert all(guard.locked(address) for address in locked)
    drive(guard, addresses(1100)[40:1024], 1)
    victim = '203.0.113.200'
    drive(guard, [victim], 5)
    assert guard.tracked_addresses() <= ADDRESS_LIMIT
    assert all(guard.locked(address) for address in locked)
    assert guard.locked(victim)
