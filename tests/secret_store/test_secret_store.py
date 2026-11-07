"""The encrypted Plex token store: owner-only files, atomic creation, fail-safe reads (S12)."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import stat
import time

from cryptography.fernet import Fernet
import pytest

from almali_library_curator import secret_store

TOKEN = 'plex-token-abcd1234wxyz'


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_a_stored_token_is_encrypted_owner_only_and_read_back(tmp_path):
    secret_store.store_plex_token(tmp_path, TOKEN)
    store, key = tmp_path / 'secrets.json', tmp_path / 'secrets.key'
    assert mode(store) == 0o600 and mode(key) == 0o600
    assert TOKEN.encode() not in store.read_bytes()
    assert TOKEN.encode() not in key.read_bytes()
    encrypted = json.loads(store.read_text())['plex_token']
    assert Fernet(key.read_bytes()).decrypt(encrypted.encode()).decode() == TOKEN
    state = secret_store.read_plex_token(tmp_path)
    assert (state.token, state.status) == (TOKEN, 'stored')
    assert TOKEN not in repr(state)


def test_nothing_stored_reads_as_absent_without_creating_files(tmp_path):
    state = secret_store.read_plex_token(tmp_path)
    assert (state.token, state.status) == ('', 'absent')
    assert list(tmp_path.iterdir()) == []


def test_forget_removes_the_token_and_keeps_the_store_readable(tmp_path):
    secret_store.store_plex_token(tmp_path, TOKEN)
    secret_store.forget_plex_token(tmp_path)
    assert secret_store.read_plex_token(tmp_path).status == 'absent'
    assert 'plex_token' not in json.loads((tmp_path / 'secrets.json').read_text())
    assert mode(tmp_path / 'secrets.json') == 0o600


def test_replacing_the_token_keeps_one_key(tmp_path):
    secret_store.store_plex_token(tmp_path, TOKEN)
    key = (tmp_path / 'secrets.key').read_bytes()
    secret_store.store_plex_token(tmp_path, 'another-token-9876')
    assert (tmp_path / 'secrets.key').read_bytes() == key
    assert secret_store.read_plex_token(tmp_path).token == 'another-token-9876'


@pytest.mark.parametrize('damage,reason', [
    ('corrupt_store', 'store_corrupt'), ('missing_key', 'key_missing'),
    ('wrong_key', 'decrypt_failed'), ('invalid_key', 'key_invalid'), ('missing_store', 'store_missing'),
])
def test_unreadable_secrets_fail_safe_with_a_log_event_and_no_secret(tmp_path, caplog, damage, reason):
    secret_store.store_plex_token(tmp_path, TOKEN)
    store, key = tmp_path / 'secrets.json', tmp_path / 'secrets.key'
    if damage == 'corrupt_store':
        store.write_text('{not json')
    elif damage == 'missing_key':
        key.unlink()
    elif damage == 'wrong_key':
        key.write_bytes(Fernet.generate_key())
    elif damage == 'invalid_key':
        key.write_bytes(b'not a fernet key')
    else:
        store.unlink()
    caplog.set_level(logging.INFO)
    state = secret_store.read_plex_token(tmp_path)
    assert (state.token, state.status) == ('', 'unreadable')
    events = [record for record in caplog.records if record.getMessage() == 'secret_store_unreadable']
    assert events and events[0].reason == reason
    assert TOKEN not in caplog.text
    assert all(TOKEN not in str(value) for record in caplog.records for value in vars(record).values())


def test_a_new_token_repairs_an_invalid_key(tmp_path):
    secret_store.store_plex_token(tmp_path, TOKEN)
    (tmp_path / 'secrets.key').write_bytes(b'not a fernet key')
    secret_store.store_plex_token(tmp_path, 'fresh-token-0000')
    assert secret_store.read_plex_token(tmp_path).token == 'fresh-token-0000'
    assert mode(tmp_path / 'secrets.key') == 0o600


def test_two_concurrent_key_creations_yield_one_key(tmp_path, monkeypatch):
    original = secret_store.os.fdopen

    def slow_fdopen(*args, **kwargs):
        time.sleep(0.05)  # widen the window between create and write
        return original(*args, **kwargs)

    monkeypatch.setattr(secret_store.os, 'fdopen', slow_fdopen)
    with ThreadPoolExecutor(8) as pool:
        keys = list(pool.map(lambda _: secret_store.ensure_key(tmp_path), range(8)))
    assert len(set(keys)) == 1
    Fernet(keys[0])  # a complete, usable key
    assert (tmp_path / 'secrets.key').read_bytes() == keys[0]
    assert mode(tmp_path / 'secrets.key') == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == ['secrets.key']


def test_concurrent_saves_leave_one_readable_token(tmp_path):
    tokens = [f'token-{index:04d}-abcdef' for index in range(8)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda token: secret_store.store_plex_token(tmp_path, token), tokens))
    state = secret_store.read_plex_token(tmp_path)
    assert state.status == 'stored' and state.token in tokens


@pytest.mark.parametrize('token,expected', [('abcdefgh12345678ab12', '••••ab12'), ('short', '••••')])
def test_the_mask_shows_at_most_the_last_four_characters(token, expected):
    assert secret_store.mask(token) == expected
