from __future__ import annotations

from pathlib import Path

def test_healthz_reports_a_ready_installation(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert set(payload) == {"status", "ready", "missing"}


def test_healthz_reports_degraded_when_the_database_is_not_migrated(app, settings):
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.database_path}{suffix}").unlink(missing_ok=True)

    response = app.test_client().get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["status"] == "degraded"
