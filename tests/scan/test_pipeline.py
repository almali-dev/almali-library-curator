"""The production pipeline with HTTP boundaries and injected enrichment."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db
from almali_library_curator.lifecycle import LifecycleEngine
from fake_plex_server import FakePlexDataset, FakePlexServer, movie
from lifecycle_helpers import make_candidate, seed_item, seed_library


NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def test_full_dry_scan_previews_without_any_live_mutation_or_external_write(tmp_path, monkeypatch):
    from almali_library_curator.scan import ScanRunner
    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    data = FakePlexDataset()
    path = tmp_path / "film.mkv"
    path.write_bytes(b"media")
    data.items["1"] = [movie("11", "Tenet", rating=7.0,
                             Part=[{"file": str(path), "size": 5}])]
    with FakePlexServer(data) as server:
        settings = Settings(config_dir=tmp_path, scheduler_enabled=False,
                            plex_base_url=server.base_url, plex_rate_limit_per_second=1000)
        save_settings(settings)
        conn = init_db(settings.database_path)
        library = seed_library(conn, "1", "Movies")
        item = seed_item(conn, library, "11", "Tenet")
        conn.execute("UPDATE media_items SET file_path = ? WHERE id = ?", (str(path), item))
        conn.commit()
        engine = LifecycleEngine(conn, settings=settings,
                                 clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
        request = engine.mark_for_removal(make_candidate(item, "11", "Tenet"))
        def protected_bytes():
            # #48: the display cache is derived from the run that is
            # published, so a dry scan publishes its rows into it too.
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT IN ('scan_runs', 'scan_run_views',"
                "                   'scan_rows', 'scan_row_categories')"
            )]
            return {table: repr([tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')
                                 if table != 'sqlite_sequence' or row[0] != 'scan_runs']).encode()
                    for table in tables}
        before = protected_bytes()
        seen = []

        def enrich(work, loaded, **services):
            seen.append(work.execute("SELECT title FROM media_items WHERE id = ?", (item,)).fetchone()[0])
            return {"linked": 0}

        report = ScanRunner(settings, clock=lambda: NOW, enrichment_step=enrich).run("manual")
        assert report.status == "finished"
        assert seen == ["Tenet"]
        plan = report.steps["deletion"]["counts"]["outcomes"]
        assert plan[0]["request_id"] == request.id
        assert plan[0]["status"] == "would_delete"
        assert plan[0]["files"] == [str(path)]
        assert plan[0]["size_bytes"] == 5
        assert all(r["method"] == "GET" for r in server.requests)
        assert path.exists()
        assert "test-token" not in json.dumps(report.steps)
        # SQLite's audit insert necessarily changes page headers. Every other
        # row's serialized bytes (and sequence entries) must remain identical.
        assert protected_bytes() == before
        conn.close()


@pytest.mark.parametrize("kill", [False, True])
def test_live_scan_rechecks_stale_pass_and_obeys_kill_switch(tmp_path, monkeypatch, kill):
    from almali_library_curator.scan import ScanRunner
    from almali_library_curator.lifecycle.safety import set_kill_switch
    from lifecycle_helpers import seed_play, seed_user

    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
    path = tmp_path / "second.mkv"
    path.write_bytes(b"media")
    data = FakePlexDataset()
    data.items["1"] = [movie("11", "First", rating=7.0), movie("12", "Second", rating=7.0,
                            Part=[{"file": str(path), "size": 5}])]
    arr = FakeRadarrDataset()
    # The deletion guard (#38) proves the Radarr resource owns every
    # marked file before any mutation, so the resource path must contain
    # the item's Plex file; the default "/movies/Second" would refuse.
    arr.movies = [movie_resource(1, "Second", path=str(path.parent))]
    arr.on_delete_movie = lambda identifier: path.unlink()
    monkeypatch.setenv("ALC_RADARR_API_KEY", arr.api_key)
    with FakePlexServer(data) as server, FakeRadarrServer(arr) as radarr:
        settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False,
                            plex_base_url=server.base_url, plex_rate_limit_per_second=1000,
                            radarr_base_url=radarr.base_url)
        save_settings(settings)
        conn = init_db(settings.database_path)
        library = seed_library(conn, "1", "Movies")
        clock = [datetime(2026, 11, 1, tzinfo=timezone.utc)]
        engine = LifecycleEngine(conn, settings=settings, clock=lambda: clock[0])
        for key, title in (("11", "First"), ("12", "Second")):
            item = seed_item(conn, library, key, title)
            engine.mark_for_removal(make_candidate(item, key, title))
        clock[0] = NOW
        engine.run_recheck()
        user = seed_user(conn, "1", "Marc")
        seed_play(conn, user, "11", NOW)
        set_kill_switch(conn, kill)

        def enrich(work, loaded, **services):
            work.execute("UPDATE media_items SET radarr_id = 1 WHERE plex_rating_key = '12'")
            work.commit()
            return {"linked": 1}

        report = ScanRunner(settings, clock=lambda: NOW, enrichment_step=enrich).run("manual")
        assert report.status == "finished"
        assert report.steps["sync"]["counts"]["media_items"] == 2
        assert report.steps["detection"]["status"] == "finished"
        assert report.steps["recheck"]["counts"]["cancelled"] == 1
        assert report.steps["recheck"]["counts"]["passed"] == 1
        assert [row[0] for row in conn.execute("SELECT state FROM removal_requests ORDER BY id")] == [
            "cancelled", "recheck_passed" if kill else "deleted"]
        assert report.steps["deletion"]["counts"]["outcomes"][0]["status"] == (
            "blocked_by_kill_switch" if kill else "deleted")
        assert path.exists() is kill
        if kill:
            assert radarr.requests == []
        else:
            event = conn.execute("SELECT * FROM removal_events WHERE outcome = 'deleted'").fetchone()
            assert event["freed_bytes"] == 5
        conn.close()


def test_a_failed_enrichment_sub_step_keeps_its_detail_and_fails_the_run(tmp_path, monkeypatch):
    """#126: a diagnostic with details vanished from the report.

    The report kept an enrichment error only when it looked like a bare class
    name, so the #120 detail ("ValidationError: <Model> <field> <type>")
    disappeared: the run said ``finished`` with ``errors: {}`` while the log
    said the Tautulli history import had failed. The run must report the step
    as failed, with that diagnostic, and never with the refused value.
    """
    from almali_library_curator.db import connect
    from almali_library_curator.scan import ScanRunner
    from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row

    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    monkeypatch.setenv("ALC_TAUTULLI_API_KEY", "test-api-key")
    plex_data = FakePlexDataset()
    plex_data.items["1"] = [movie("11", "Tenet")]
    tautulli_data = FakeTautulliDataset()
    refused = history_row(1, 11, 1, "Marc")
    refused["rating_key"] = "refused-value-1234"
    tautulli_data.history = [refused]
    expected = "ValidationError: TautulliHistoryEntry rating_key int_parsing"
    with FakePlexServer(plex_data) as plex, FakeTautulliServer(tautulli_data) as tautulli:
        settings = Settings(config_dir=tmp_path, scheduler_enabled=False,
                            plex_base_url=plex.base_url, plex_rate_limit_per_second=1000,
                            tautulli_base_url=tautulli.base_url)
        save_settings(settings)
        init_db(settings.database_path).close()
        report = ScanRunner(settings, clock=lambda: NOW).run("manual")
        stored_conn = connect(settings.database_path)
        try:
            stored = stored_conn.execute(
                "SELECT status, error, report FROM scan_runs WHERE id = ?",
                (report.id,)).fetchone()
        finally:
            stored_conn.close()

    assert report.status == "failed"
    assert report.steps["enrichment"]["status"] == "failed"
    assert report.steps["enrichment"]["error"] == expected
    assert report.steps["enrichment"]["counts"]["errors"] == {
        "import_tautulli_history": expected}
    # The failed history import still blocks this run's deletion (#49).
    assert report.steps["deletion"]["status"] == "skipped"
    assert stored["status"] == "failed"
    assert stored["error"] == expected
    assert json.loads(stored["report"])["enrichment"]["counts"]["errors"] == {
        "import_tautulli_history": expected}
    assert "refused-value-1234" not in json.dumps(report.steps) + stored["error"] + stored["report"]


def test_counts_keeps_the_diagnostic_shapes_and_no_external_text():
    """#126: only what ``error_diagnostic`` writes is stored, nothing else."""
    from almali_library_curator.scan.pipeline import counts

    kept = {
        "link_arr.sonarr": "ConnectionError",
        "import_tautulli_history": (
            "ValidationError: TautulliHistoryEntry parent_media_index int_parsing;"
            " media_index int_parsing"),
        "refresh_files": "ValidationError: TautulliHistoryEntry rating_key.? int_parsing",
        "detection": "ValidationError: PlexMetadata ? int_parsing",
        "build_enrichers": "ValidationError: PlexMetadata ratingKey missing; +4 more",
        "sync": "ValidationError: Thing - missing",
    }
    dropped = [
        "OSError('sensitive-diagnostic')",
        "rejected test-api-key sensitive-diagnostic",
        "Tautulli failed get_history: Not a valid date format.",
        "ValidationError: SecretModel field ? int_parsing; +nope",
        "sensitive-diagnostic",
    ]
    summary = {"linked": 2, "errors": dict(kept, **{f"step{index}": text
                                                   for index, text in enumerate(dropped)})}

    assert counts(summary) == {"linked": 2, "errors": kept}
