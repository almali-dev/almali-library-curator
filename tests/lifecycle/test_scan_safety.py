"""T8 lifecycle safety contracts through the public APIs."""
from dataclasses import replace

from lifecycle_helpers import seed_play, seed_user
from test_deletion import Scenario


def test_dry_mark_skips_collection_until_live_retry(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, dry_run=True)
    request = scenario.add(pass_check=False)
    assert scenario.collections.ensure_calls == []
    assert scenario.row(request)["collection_added"] == 0
    assert scenario.engine.retry_collection_additions() == 0
    scenario.engine.settings = replace(scenario.settings, dry_run=False)
    assert scenario.engine.retry_collection_additions() == 1
    assert scenario.collections.holds("item-1")


def test_dry_recheck_previews_pass_and_cancel_without_any_write(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1, pass_check=False)
    second = scenario.add(2, pass_check=False)
    scenario.clock.advance(days=14)
    user = seed_user(conn, "1", "Marc")
    seed_play(conn, user, "item-2", scenario.clock())
    scenario.engine.settings = replace(scenario.settings, dry_run=True)
    before = conn.serialize()
    members = repr(scenario.collections)
    report = scenario.engine.run_recheck()
    assert [o.request.id for o in report.passed] == [first.id]
    assert [o.request.id for o in report.cancelled] == [second.id]
    assert "Marc" in report.cancelled[0].cancel_reason
    assert conn.serialize() == before
    assert repr(scenario.collections) == members


def test_stale_pass_is_rechecked_and_cancels_after_new_play(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    user = seed_user(conn, "1", "Marc")
    seed_play(conn, user, "item-1", scenario.clock())
    report = scenario.engine.run_recheck()
    assert [o.request.id for o in report.cancelled] == [request.id]
    assert scenario.row(request)["state"] == "cancelled"


def test_executor_requires_explicit_same_run_ids(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    with scenario.executor() as (executor, radarr, _):
        assert executor.run_deletions().outcomes == ()
        assert executor.run_deletions(passed_ids=[]).outcomes == ()
        assert radarr.requests == []
        fresh = scenario.engine.run_recheck()
        report = executor.run_deletions(passed_ids=[o.request.id for o in fresh.passed])
        assert report.outcomes[0].status == "deleted"
    assert scenario.row(request)["state"] == "deleted"


def test_dry_deletion_plan_includes_files_and_size_without_writes(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, dry_run=True)
    request = scenario.add()
    before = conn.serialize()
    with scenario.executor() as (executor, radarr, sonarr):
        outcome = executor.run_deletions(passed_ids=[request.id]).outcomes[0]
        assert outcome.status == "would_delete"
        assert outcome.files == (str(scenario.paths[1][0]),)
        assert outcome.size_bytes == 17
        assert radarr.requests == sonarr.requests == []
    assert conn.serialize() == before


def test_kill_switch_is_read_again_between_batch_claims(conn, tmp_path):
    from almali_library_curator.lifecycle.safety import kill_switch_enabled, set_kill_switch
    from almali_library_curator.db import connect

    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1)
    second = scenario.add(2)
    assert kill_switch_enabled(conn) is False
    original = scenario.radarr.on_delete_movie
    database = conn.execute("PRAGMA database_list").fetchone()["file"]

    def delete(identifier):
        original(identifier)
        other = connect(database)
        set_kill_switch(other, True)
        other.close()

    scenario.radarr.on_delete_movie = delete
    with scenario.executor() as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=[first.id, second.id])
        assert [o.status for o in report.outcomes] == ["deleted", "blocked_by_kill_switch"]
        assert not any(r["path"] == "/api/v3/movie/2" for r in radarr.requests)
        assert scenario.row(second)["state"] == "recheck_passed"
        set_kill_switch(conn, False)
        assert executor.run_deletions(passed_ids=[second.id]).outcomes[0].status == "deleted"


def test_switch_flipped_during_file_preflight_wins_before_claim(conn, tmp_path):
    import os
    from almali_library_curator.lifecycle.safety import set_kill_switch

    scenario = Scenario(conn, tmp_path)
    request = scenario.add()

    def stat(path):
        set_kill_switch(conn, True)
        return os.stat(path)

    with scenario.executor(stat=stat) as (executor, radarr, sonarr):
        outcome = executor.run_deletions(passed_ids=[request.id]).outcomes[0]
        assert outcome.status == "blocked_by_kill_switch"
        assert radarr.requests == sonarr.requests == []
    assert scenario.row(request)["state"] == "recheck_passed"
