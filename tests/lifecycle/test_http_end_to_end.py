"""S18: the owner HTTP workflow reaches a verified live deletion and History."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

from almali_library_curator.app import create_app
from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import connect
from almali_library_curator.scan import ScanRunner
from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from lifecycle_helpers import FrozenClock


def _scan_over_http(client, runner, conn):
    response = client.post("/scan")
    assert response.status_code == 202
    assert response.get_json() == {"status": "accepted"}
    runner.thread.join(10)
    assert not runner.thread.is_alive(), "background scan did not finish"
    run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "finished", dict(run)
    assert run["trigger"] == "manual"
    assert run["mode"] == "live"
    report = json.loads(run["report"])
    assert all(step["status"] == "finished" for step in report.values()), report
    return report


def test_scan_mark_grace_scan_verified_delete_and_history(tmp_path, monkeypatch, login):
    media = tmp_path / "media"
    media.mkdir()
    file = media / "Tenet.mkv"
    file.write_bytes(b"verified media")
    size = file.stat().st_size
    data = FakePlexDataset()
    data.items["1"] = [movie("11", "Tenet", Part=[{"file": str(file), "size": size}])]
    arr = FakeRadarrDataset()
    # The live #38 guard must prove that Radarr owns the Plex file.
    arr.movies = [movie_resource(1, "Tenet", path=str(media))]
    arr.on_delete_movie = lambda _identifier: file.unlink()
    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    monkeypatch.setenv("ALC_RADARR_API_KEY", arr.api_key)
    clock = FrozenClock(datetime(2026, 11, 2, 12, tzinfo=timezone.utc))
    monkeypatch.setattr("almali_library_curator.web.actions.now", clock)
    session_time = clock().timestamp()

    def enrich(work, loaded, **services):
        # Keep the real pipeline; inject only the supported external-ID linker.
        work.execute("UPDATE media_items SET radarr_id = 1 WHERE plex_rating_key = '11'")
        work.commit()
        return {"linked": 1}

    with FakePlexServer(data) as plex, FakeRadarrServer(arr) as radarr:
        settings = Settings(
            config_dir=tmp_path / "config", dry_run=False, scheduler_enabled=False,
            plex_base_url=plex.base_url, plex_rate_limit_per_second=1000,
            radarr_base_url=radarr.base_url, media_root=str(media),
        )
        save_settings(settings)
        runner = ScanRunner(settings, clock=clock, enrichment_step=enrich)
        # Session expiry is independent of the four-day lifecycle advance.
        app = create_app(settings, scan_runner=runner, wall_clock=lambda: session_time)
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        assert login(client).status_code == 303

        with closing(connect(settings.database_path)) as conn:
            first = _scan_over_http(client, runner, conn)
            assert first["deletion"]["counts"]["count"] == 0
            item = conn.execute("SELECT id FROM media_items WHERE plex_rating_key = '11'").fetchone()[0]
            response = client.post(f"/items/{item}/mark", data={"unit": "movie", "grace_days": "3"})
            assert response.status_code == 303
            marked = conn.execute("SELECT * FROM removal_requests").fetchone()
            assert marked["media_item_id"] == item
            assert marked["state"] == "marked"
            assert marked["grace_days"] == 3
            assert datetime.fromisoformat(marked["due_at"]) == clock() + timedelta(days=3)
            assert file.exists()
            assert not any(request["method"] == "DELETE" for request in radarr.requests)
            assert conn.execute("SELECT COUNT(*) FROM removal_events").fetchone()[0] == 0

            clock.advance(days=4)
            second = _scan_over_http(client, runner, conn)
            assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2
            assert second["recheck"]["counts"]["passed"] == 1
            outcomes = second["deletion"]["counts"]["outcomes"]
            assert len(outcomes) == 1
            assert outcomes[0]["request_id"] == marked["id"]
            assert outcomes[0]["status"] == "deleted"
            assert outcomes[0]["freed_bytes"] == size
            deleted = conn.execute("SELECT * FROM removal_requests WHERE id = ?", (marked["id"],)).fetchone()
            assert deleted["state"] == "deleted"
            assert deleted["recheck_status"] == "passed"
            assert deleted["last_error"] is None
            assert datetime.fromisoformat(deleted["deleted_at"]) == clock()
            assert not file.exists()
            deletes = [request for request in radarr.requests if request["method"] == "DELETE"]
            assert len(deletes) == 1
            assert deletes[0]["path"] == "/api/v3/movie/1"
            assert deletes[0]["query"]["deleteFiles"] == "true"
            event = conn.execute("SELECT * FROM removal_events").fetchone()
            assert event["removal_request_id"] == marked["id"]
            assert event["outcome"] == "deleted"
            assert event["title"] == "Tenet"
            assert event["freed_bytes"] == size

        history = client.get("/history?lang=en")
        assert history.status_code == 200  # Still authenticated, without another login.
        page = history.get_data(as_text=True)
        assert '<h3 class="candidate-title">Tenet</h3>' in page
        assert 'class="candidate-row card history-row"' in page
