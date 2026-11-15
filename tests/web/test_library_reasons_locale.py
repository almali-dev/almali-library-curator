"""S1b and S1d (#47): the Library and Protected reasons follow the request's
language, and a row written before reasons carried parts keeps its text.

The scan runs with ``settings.locale = "en"``, so the stored text is English;
the pages then answer in the language the request asks for, with the dates
written the way each catalog writes them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from fake_plex_server import FakePlexDataset, FakePlexServer, movie

from almali_library_curator.app import create_app
from almali_library_curator.config import save_settings
from almali_library_curator.db import connect
from almali_library_curator.scan import ScanRunner

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
# 2024-01-15: a month, and a year, each catalog writes its own way.
ADDED_AT = 1705276800
TOKEN = 'test-token'

CANDIDATE_REASON = {
    'en': 'Added January 2024. 0 plays by any of the 2 users.',
    'ca': 'Afegida (gener de 2024). Cap reproducció per cap dels 2 usuaris.',
    'es': 'Añadida en enero de 2024. Ninguna reproducción por ninguno de los 2 usuarios.',
}

PROTECTED_REASON = {
    'en': 'Rated 8.7, at or above the minimum of 8.0.',
    'ca': 'Amb una valoració de 8.7, igual o superior al mínim de 8.0.',
    'es': 'Con una valoración de 8.7, igual o superior al mínimo de 8.0.',
}


@pytest.fixture
def scanned(settings, owner_client_class, monkeypatch):
    """A finished scan in English over the fake Plex server, and its client."""
    monkeypatch.setenv('ALC_PLEX_TOKEN', TOKEN)
    data = FakePlexDataset()
    data.items['1'] = [movie('11', 'Tenet', rating=7.0, addedAt=ADDED_AT),
                       movie('12', 'Severance', rating=8.7, addedAt=ADDED_AT)]
    with FakePlexServer(data) as server:
        settings = replace(settings, locale='en', scheduler_enabled=False,
                           plex_base_url=server.base_url, plex_rate_limit_per_second=1000)
        save_settings(settings)
        app = create_app(settings)
        app.config.update(TESTING=True, SECRET_KEY='web-test-session', WTF_CSRF_ENABLED=False)
        app.test_client_class = owner_client_class
        report = ScanRunner(settings, clock=lambda: NOW,
                            enrichment_step=lambda *args, **kwargs: {}).run('manual')
        assert report.status == 'finished'
        db = connect(settings.database_path)
        yield app.test_client(), db
        db.close()


def page(client, tab, locale):
    return client.get(f'/?tab={tab}&lang={locale}').get_data(as_text=True)


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_library_reasons_are_rendered_in_the_request_language(scanned, locale):
    client, _ = scanned

    html = page(client, 'candidates', locale)

    assert CANDIDATE_REASON[locale] in html
    for other, sentence in CANDIDATE_REASON.items():
        if other != locale:
            assert sentence not in html, (locale, other)


@pytest.mark.parametrize('locale', ('en', 'ca', 'es'))
def test_protected_reasons_are_rendered_in_the_request_language(scanned, locale):
    client, _ = scanned

    html = page(client, 'protected', locale)

    assert PROTECTED_REASON[locale] in html
    for other, sentence in PROTECTED_REASON.items():
        if other != locale:
            assert sentence not in html, (locale, other)


def test_the_scan_snapshot_carries_the_raw_parts(scanned):
    _, db = scanned

    snapshot = json.loads(db.execute('SELECT snapshot FROM scan_run_views').fetchone()['snapshot'])

    [candidate] = snapshot['counts']['candidate_rows']
    assert candidate['reason_parts'][0]['key'] == 'detection.reason.never_watched.movie'
    assert candidate['reason_parts'][0]['params']['users'] == 2
    [protected] = snapshot['counts']['protected_rows']
    assert protected['protection_reason_parts'][0]['key'] == 'protection.reason.rating'


def _drop_parts(db):
    """Rewrite the stored snapshot the way a pre-#47 row was written."""
    row = db.execute('SELECT run_id, snapshot FROM scan_run_views').fetchone()
    snapshot = json.loads(row['snapshot'])
    for section in ('candidate_rows', 'protected_rows'):
        for item in snapshot['counts'][section]:
            item.pop('reason_parts', None)
            item.pop('protection_reason_parts', None)
    db.execute('UPDATE scan_run_views SET snapshot = ? WHERE run_id = ?',
               (json.dumps(snapshot), row['run_id']))
    db.commit()


def test_a_part_that_cannot_be_rendered_keeps_the_stored_text(scanned):
    client, db = scanned
    row = db.execute('SELECT run_id, snapshot FROM scan_run_views').fetchone()
    snapshot = json.loads(row['snapshot'])
    for item in snapshot['counts']['candidate_rows']:
        item['reason_parts'] = [{'params': {'users': 2}}]
    db.execute('UPDATE scan_run_views SET snapshot = ? WHERE run_id = ?',
               (json.dumps(snapshot), row['run_id']))
    db.commit()

    html = page(client, 'candidates', 'ca')

    assert CANDIDATE_REASON['en'] in html


def test_a_row_without_parts_keeps_its_stored_text(scanned):
    client, db = scanned
    _drop_parts(db)

    candidates = page(client, 'candidates', 'ca')
    protected = page(client, 'protected', 'ca')

    assert CANDIDATE_REASON['en'] in candidates
    assert CANDIDATE_REASON['ca'] not in candidates
    assert PROTECTED_REASON['en'] in protected
    assert PROTECTED_REASON['ca'] not in protected
