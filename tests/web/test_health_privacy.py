"""Public health remains useful without exposing version fingerprints (#30)."""
from flask.testing import FlaskClient
import pytest

from almali_library_curator import app as app_module


@pytest.mark.parametrize('database_ready', [True, False])
@pytest.mark.parametrize('signed_in', [False, True])
def test_health_payload_only_exposes_liveness_and_readiness(app, client, monkeypatch, database_ready, signed_in):
    monkeypatch.setattr(app_module, '_database_ready', lambda path: database_ready)
    browser = client if signed_in else FlaskClient(app)
    response = browser.get('/healthz')
    assert response.status_code == (200 if database_ready else 503)
    assert response.get_json() == {
        'status': 'ok' if database_ready else 'degraded',
        'ready': False,
        'missing': ['plex'],
    }
    assert 'version' not in response.get_json()
    assert 'schema_version' not in response.get_json()
