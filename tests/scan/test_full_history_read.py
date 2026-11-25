"""#112: a scan reads the whole history of Plex and Tautulli, every time.

#74 asked Plex only for what followed the newest stored play and Tautulli
only for the days since the newest stored one, so a play either service
stored late with an older instant was skipped for good. The scan now reads
the whole history through the real pipeline and the fake servers: the same
history read again must add no play and change no count the app shows.
"""

from __future__ import annotations

from datetime import datetime, timezone

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import connect, init_db
from almali_library_curator.web.views import sources_view
from fake_plex_server import FakePlexDataset, FakePlexServer, history_entry, movie
from fake_tautulli_server import FakeTautulliDataset, FakeTautulliServer, history_row

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
APRIL = 1_711_929_600  # 2024-04-01
MAY = 1_714_521_600  # 2024-05-01
PLEX_NEWEST = 1_700_000_000

#: The columns of a stored play, without the id every scan creates its own of.
PLAY_COLUMNS = (
    "user_id, plex_rating_key, watched, played_at, position_ms, duration_ms, source"
)


def plays(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute(
        f"SELECT {PLAY_COLUMNS} FROM play_events ORDER BY played_at, id"
    )]


def test_a_second_scan_of_the_same_history_adds_no_play_and_changes_no_count(
    tmp_path, monkeypatch
):
    from almali_library_curator.scan import ScanRunner

    monkeypatch.setenv("ALC_PLEX_TOKEN", "test-token")
    monkeypatch.setenv("ALC_TAUTULLI_API_KEY", "test-api-key")
    plex_data = FakePlexDataset()
    plex_data.items["1"] = [movie("11", "Tenet"), movie("12", "Other")]
    plex_data.accounts = [{"id": 1, "name": "Marc"}]
    # Five plays, and a page of two: a read that starts from the newest stored
    # play ends long before the oldest one.
    plex_data.history = [
        history_entry("11", 1, PLEX_NEWEST - 60 * number, 6_000_000, 6_000_000)
        for number in range(5)
    ]
    tautulli_data = FakeTautulliDataset()
    tautulli_data.history = [
        history_row(1, 12, 1, "Marc", date=APRIL, started=APRIL),
        history_row(2, 12, 1, "Marc", date=MAY, started=MAY),
    ]
    with FakePlexServer(plex_data) as plex, FakeTautulliServer(tautulli_data) as tautulli:
        settings = Settings(
            config_dir=tmp_path, dry_run=False, scheduler_enabled=False,
            plex_base_url=plex.base_url, plex_rate_limit_per_second=1000,
            plex_page_size=2, tautulli_base_url=tautulli.base_url,
        )
        save_settings(settings)
        init_db(settings.database_path).close()
        runner = ScanRunner(settings, clock=lambda: NOW)

        first = runner.run("manual")
        conn = connect(settings.database_path)
        try:
            rows_before = plays(conn)
            viewers_before = sources_view(conn).plex_users
            stored_before = conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0]
        finally:
            conn.close()

        second = runner.run("manual")
        conn = connect(settings.database_path)
        try:
            rows_after = plays(conn)
            viewers_after = sources_view(conn).plex_users
            stored_after = conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0]
        finally:
            conn.close()

    assert (first.status, second.status) == ("finished", "finished")
    # The whole history, read on both scans: five Plex plays and two Tautulli
    # ones, and only one scan inserted each of them.
    assert len(rows_before) == 7
    assert first.steps["sync"]["counts"]["play_events"] == 5
    assert first.steps["enrichment"]["counts"]["history_imported"] == 2
    # Read again, nothing new and nothing twice.
    assert second.steps["sync"]["counts"]["play_events"] == 5
    assert second.steps["enrichment"]["counts"]["history_imported"] == 0
    assert rows_after == rows_before
    assert stored_after == stored_before == 7
    assert viewers_after == viewers_before == 1
