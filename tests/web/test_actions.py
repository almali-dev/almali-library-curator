"""Owner actions exercise real engines through the Flask client."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from almali_library_curator.config import save_settings
from lifecycle_helpers import seed_item, seed_library
from almali_library_curator.lifecycle.safety import kill_switch_enabled


def item(conn, *, rating=7.0):
    library = seed_library(conn, '1', 'Movies')
    return seed_item(conn, library, '11', 'Tenet', rating=rating)


def flashes(client):
    with client.session_transaction() as session:
        return [key for _, key in session.get('_flashes', [])]


def mark(client, identifier, **data):
    return client.post(f'/items/{identifier}/mark', data={'unit': 'movie', **data})


def test_mark_rederives_without_a_scan_and_stores_owner_and_grace(client, conn):
    identifier = item(conn)
    response = mark(client, identifier, grace_days='30', marked_by='untrusted')
    assert response.status_code == 303
    row = conn.execute('SELECT * FROM removal_requests').fetchone()
    assert row['grace_days'] == 30 and row['marked_by'] == 'owner'
    assert flashes(client) == ['web.flash.marked']


def test_duplicate_mark_is_refused_without_a_second_request(client, conn):
    identifier = item(conn)
    mark(client, identifier)
    response = mark(client, identifier)
    assert response.status_code == 303
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 1
    assert flashes(client)[-1] == 'web.flash.duplicate_mark'


def test_new_protection_is_rechecked_at_action_time(client, conn):
    identifier = item(conn, rating=8.7)
    assert mark(client, identifier).status_code == 303
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
    assert flashes(client)[-1] == 'web.flash.protected'


@pytest.mark.parametrize('days', ['2', '61', 'invalid'])
def test_invalid_grace_is_refused_atomically(client, conn, days):
    identifier = item(conn)
    assert mark(client, identifier, grace_days=days).status_code == 303
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
    assert flashes(client)[-1] == 'web.flash.grace_range'


def test_stale_candidate_no_longer_matching_is_not_marked(client, conn):
    identifier = item(conn)
    conn.execute('UPDATE media_items SET added_at=? WHERE id=?',
                 (datetime.now(timezone.utc).isoformat(), identifier))
    conn.commit()
    assert mark(client, identifier).status_code == 303
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
    assert flashes(client)[-1] == 'web.flash.not_candidate'


@pytest.mark.parametrize('data', [{'unit': 'wrong'}, {'unit': 'seasons', 'seasons': '2'},
                                  {'unit': 'movie', 'seasons': '1'}, {'unit': 'seasons', 'seasons': 'bad'}])
def test_tampered_scope_is_refused(client, conn, data):
    identifier = item(conn)
    response = client.post(f'/items/{identifier}/mark', data=data)
    assert response.status_code == 303
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
    assert flashes(client)[-1] == 'web.flash.invalid_scope'


@pytest.mark.parametrize('action,key', [('undo', 'undone'), ('keep', 'kept')])
def test_undo_and_keep_cancel_the_real_request(client, conn, action, key):
    identifier = item(conn)
    mark(client, identifier)
    request_id = conn.execute('SELECT id FROM removal_requests').fetchone()[0]
    response = client.post(f'/requests/{request_id}/{action}')
    assert response.status_code == 303
    assert conn.execute('SELECT state FROM removal_requests').fetchone()[0] == 'cancelled'
    assert conn.execute("SELECT count(*) FROM removal_events WHERE outcome='cancelled'").fetchone()[0] == 1
    assert flashes(client)[-1] == 'web.flash.' + key


@pytest.mark.parametrize('state', ['deleting', 'deleted', 'cancelled'])
@pytest.mark.parametrize('action', ['undo', 'keep'])
def test_terminal_or_claimed_request_refuses_cancellation(client, conn, state, action):
    identifier = item(conn)
    mark(client, identifier)
    conn.execute('UPDATE removal_requests SET state=?', (state,))
    conn.commit()
    assert client.post(f'/requests/1/{action}').status_code == 303
    assert conn.execute('SELECT state FROM removal_requests').fetchone()[0] == state
    assert flashes(client)[-1] == 'web.flash.request_state'


def test_unreadable_request_action_is_refused_safely(client, conn):
    identifier = item(conn)
    mark(client, identifier)
    conn.execute("UPDATE candidates SET facts='broken-json'")
    conn.commit()
    assert client.post('/requests/1/undo').status_code == 303
    assert flashes(client)[-1] == 'web.flash.request_state'


def test_keep_forever_and_undo_change_only_protection_never_mark(client, conn):
    identifier = item(conn)
    assert client.post(f'/items/{identifier}/keep-forever', data={'unit': 'movie'}).status_code == 303
    assert conn.execute('SELECT kind FROM protections').fetchone()[0] == 'keep_forever'
    assert flashes(client)[-1] == 'web.flash.keep_forever'
    # Undo remains possible even when the item no longer matches a category.
    conn.execute('UPDATE media_items SET added_at=?', (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    assert client.post(f'/items/{identifier}/undo-keep-forever', data={'unit': 'movie'}).status_code == 303
    assert conn.execute('SELECT count(*) FROM protections').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM removal_requests').fetchone()[0] == 0
    assert flashes(client)[-1] == 'web.flash.keep_forever_undone'


@pytest.mark.parametrize('accepted,key', [(True, 'scan_started'), (False, 'scan_running')])
def test_run_scan_action_redirects_with_observed_result(app, client, accepted, key):
    class Runner:
        def start_background(self):
            return accepted
    app.extensions['scan_runner'] = Runner()
    assert client.post('/actions/scan').status_code == 303
    assert flashes(client)[-1] == 'web.flash.' + key


def test_stopping_the_deletions_persists_immediately(client, conn):
    assert client.post('/settings/kill-switch', data={'enabled': 'on'}).status_code == 303
    assert kill_switch_enabled(conn) is True
    assert flashes(client)[-1] == 'web.flash.kill_switch_on'


def test_resuming_the_deletions_asks_first_and_only_then_persists(client, conn):
    """#20: arming the deletions again needs a second, explicit step."""
    assert client.post('/settings/kill-switch', data={'enabled': 'on'}).status_code == 303
    assert kill_switch_enabled(conn) is True

    confirmation = client.post('/settings/kill-switch', data={'enabled': 'off'})
    assert confirmation.status_code == 200
    assert kill_switch_enabled(conn) is True

    assert client.post('/settings/kill-switch/confirm').status_code == 303
    assert kill_switch_enabled(conn) is False
    assert flashes(client)[-1] == 'web.flash.kill_switch_off'


def test_invalid_kill_switch_does_not_change_state(client, conn):
    assert client.post('/settings/kill-switch', data={'enabled': 'invalid'}).status_code == 303
    assert kill_switch_enabled(conn) is False
    assert flashes(client)[-1] == 'web.flash.invalid_scope'


@pytest.mark.parametrize('path', ['/items/1/mark', '/items/1/keep-forever', '/items/1/undo-keep-forever',
                                 '/requests/1/undo', '/requests/1/keep', '/actions/scan', '/settings/kill-switch'])
def test_mutating_routes_reject_get(client, path):
    assert client.get(path).status_code == 405


def test_missing_item_or_request_is_a_localized_refusal(client, conn):
    assert mark(client, 999).status_code == 303
    assert flashes(client)[-1] == 'web.flash.invalid_scope'
    assert client.post('/requests/999/undo').status_code == 303
    assert flashes(client)[-1] == 'web.flash.request_state'
