"""Verified deletion through the public lifecycle API, HTTP fakes and real files."""

from contextlib import contextmanager
import logging
import os

import pytest

from almali_library_curator.lifecycle import LifecycleEngine, RequestStateError
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from almali_library_curator.detection.models import Unit
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from lifecycle_helpers import (
    FakeCollections, FrozenClock, default_settings, make_candidate,
    seed_episode, seed_item, seed_library, seed_play, seed_user,
)


class Scenario:
    def __init__(self, conn, tmp_path, *, unit=Unit.MOVIE, dry_run=False):
        self.conn = conn
        self.root = tmp_path
        self.clock = FrozenClock()
        self.collections = FakeCollections()
        self.settings = default_settings(dry_run=dry_run)
        self.engine = LifecycleEngine(conn, settings=self.settings, clock=self.clock,
                                      collections=self.collections)
        self.unit = unit
        self.library = seed_library(conn, "1", "Library")
        self.radarr = FakeRadarrDataset()
        self.sonarr = FakeSonarrDataset()
        self.paths = {}
        self.radarr.on_delete_movie = lambda identifier: self.paths[identifier][0].unlink(missing_ok=True)
        self.sonarr.on_delete_series = self.delete_series_files
        self.sonarr.on_delete_episode_files = self.delete_episode_files
        self.episode_paths = {}
        self.passed_ids = []

    def recheck(self):
        report = self.engine.run_recheck()
        self.passed_ids = [outcome.request.id for outcome in report.passed]
        return report

    def delete_series_files(self, identifier):
        for path in self.paths[identifier]:
            path.unlink(missing_ok=True)

    def delete_episode_files(self, identifiers):
        for identifier in identifiers:
            self.episode_paths[identifier].unlink(missing_ok=True)

    def add(self, identifier=1, *, pass_check=True):
        key = f"item-{identifier}"
        title = f"Title {identifier}"
        item = seed_item(self.conn, self.library, key, title,
                         item_type="movie" if self.unit is Unit.MOVIE else "series")
        paths = []
        if self.unit is Unit.MOVIE:
            path = self.root / f"{identifier}.mkv"
            path.write_bytes(b"x" * 17)
            paths.append(path)
            self.conn.execute("UPDATE media_items SET file_path = ?, radarr_id = ? WHERE id = ?",
                              (str(path), identifier, item))
            self.radarr.movies.append(movie_resource(identifier, title))
        else:
            self.conn.execute("UPDATE media_items SET sonarr_id = ? WHERE id = ?", (identifier, item))
            self.sonarr.series.append(series_resource(identifier, title))
            for season in (1, 2):
                season_key = f"{key}-s{season}"
                seed_item(self.conn, self.library, season_key, f"Season {season}",
                          item_type="season", parent_rating_key=key, season_number=season)
                episode_key = seed_episode(self.conn, key, season, 1)
                path = self.root / f"{identifier}-s{season}.mkv"
                path.write_bytes(b"x" * 17)
                paths.append(path)
                self.conn.execute("UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                                  (str(path), episode_key))
                file_id = identifier * 10 + season
                self.sonarr.episode_files.append({"id": file_id, "seriesId": identifier, "seasonNumber": season})
                self.episode_paths[file_id] = path
        self.paths[identifier] = paths
        self.conn.commit()
        candidate = make_candidate(item, key, title, unit=self.unit,
                                   seasons=(1,) if self.unit is Unit.SEASONS else ())
        request = self.engine.mark_for_removal(candidate, marked_by="owner")
        if pass_check:
            self.clock.advance(days=14)
            self.recheck()
        return request

    @contextmanager
    def executor(self, **options):
        from almali_library_curator.lifecycle.deletion import DeletionExecutor

        with FakeRadarrServer(self.radarr) as radarr, FakeSonarrServer(self.sonarr) as sonarr:
            executor = DeletionExecutor(
                self.engine,
                radarr=RadarrClient(radarr.base_url, self.radarr.api_key,
                                    rate_limit_per_second=0, max_retries=0),
                sonarr=SonarrClient(sonarr.base_url, self.sonarr.api_key,
                                    rate_limit_per_second=0, max_retries=0),
                **options,
            )
            yield executor, radarr, sonarr

    def row(self, request):
        return self.conn.execute("SELECT * FROM removal_requests WHERE id = ?", (request.id,)).fetchone()


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES, Unit.SEASONS])
def test_executor_calls_arr_in_order_and_deletes_only_its_scope(conn, tmp_path, caplog, unit):
    caplog.set_level(logging.INFO)
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    before = scenario.radarr.on_delete_movie if unit is Unit.MOVIE else scenario.sonarr.on_delete_series
    observed_start = []

    def delete(identifier):
        observed_start.append(any(record.message == "removal_deletion_started" for record in caplog.records))
        before(identifier)

    if unit is Unit.MOVIE:
        scenario.radarr.on_delete_movie = delete
    elif unit is Unit.SERIES:
        scenario.sonarr.on_delete_series = delete
    with scenario.executor() as (executor, radarr, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert [outcome.status for outcome in report.outcomes] == ["deleted"]
        server = radarr if unit is Unit.MOVIE else sonarr
        writes = [(r["method"], r["path"]) for r in server.requests if r["method"] != "GET"]
        resource = "movie" if unit is Unit.MOVIE else "series"
        if unit is Unit.SEASONS:
            assert writes == [("PUT", "/api/v3/series/1"), ("DELETE", "/api/v3/episodefile/bulk")]
            assert scenario.paths[1][1].is_file()
            assert scenario.sonarr.series[0]["monitored"] is True
            assert [s["monitored"] for s in scenario.sonarr.series[0]["seasons"]] == [False, True]
            assert scenario.sonarr.import_exclusions == []
        else:
            assert writes == [("PUT", f"/api/v3/{resource}/1"),
                              ("POST", "/api/v3/importlistexclusion"),
                              ("DELETE", f"/api/v3/{resource}/1")]
            assert observed_start == [True]
            delete_request = server.requests_for(f"/api/v3/{resource}/1")[-1]
            assert delete_request["query"]["deleteFiles"] == "true"
            exclusions = scenario.radarr.import_exclusions if unit is Unit.MOVIE else scenario.sonarr.import_exclusions
            external_id = "tmdbId" if unit is Unit.MOVIE else "tvdbId"
            assert exclusions[0][external_id] == (2001 if unit is Unit.MOVIE else 1001)
    assert not scenario.paths[1][0].exists()
    assert scenario.row(request)["state"] == "deleted"
    assert scenario.engine.open_requests() == []


def test_failure_does_not_starve_later_requests_and_retry_requires_recheck(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1)
    second = scenario.add(2)
    scenario.radarr.fail_always("PUT /api/v3/movie/1", 503)
    with scenario.executor() as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert [o.request.id for o in report.outcomes] == [first.id, second.id]
        assert [o.status for o in report.outcomes] == ["failed", "deleted"]
        assert scenario.row(first)["state"] == "removal_failed"
        assert scenario.row(first)["last_error"]
        assert scenario.collections.holds("item-1")
        scenario.radarr.always_fail.clear()
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
        scenario.recheck()
        assert [o.status for o in executor.run_deletions(passed_ids=scenario.passed_ids).outcomes] == ["deleted"]


@pytest.mark.parametrize("failure", ["present", "unknown", "empty", "unreachable", "auth", "exclusion", "delete"])
def test_only_confirmed_missing_files_allow_deleted(conn, tmp_path, caplog, failure):
    caplog.set_level(logging.INFO)
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    stat_calls = []

    def stat(target):
        stat_calls.append(str(target))
        if failure == "unknown" and len(stat_calls) > 1:
            raise PermissionError("secret-must-not-be-stored")
        return os.stat(target)

    if failure == "present":
        scenario.radarr.on_delete_movie = lambda identifier: None
    elif failure == "empty":
        conn.execute("UPDATE media_items SET file_path = NULL")
        conn.commit()
    elif failure in ("auth", "exclusion", "delete"):
        endpoint = {"auth": "PUT /api/v3/movie/1", "exclusion": "POST /api/v3/importlistexclusion",
                    "delete": "DELETE /api/v3/movie/1"}[failure]
        scenario.radarr.fail_always(endpoint, 401 if failure == "auth" else 500)
    with scenario.executor(stat=stat) as (executor, radarr, _):
        if failure == "unreachable":
            radarr.stop()
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert report.outcomes[0].status == "failed"
        assert report.outcomes[0].freed_bytes == 0
        assert scenario.row(request)["state"] == "removal_failed"
        assert scenario.row(request)["deleted_at"] is None
        assert scenario.row(request)["last_error"]
        assert scenario.collections.holds("item-1")
        event = conn.execute("SELECT * FROM removal_events").fetchone()
        assert event["outcome"] == "failed"
        assert event["error"] == scenario.row(request)["last_error"]
        assert event["freed_bytes"] == 0
        if failure == "empty":
            assert radarr.requests == []
    assert "secret-must-not-be-stored" not in str(list(conn.iterdump())) + caplog.text
    assert not any(record.message == "removal_deleted" for record in caplog.records)


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES, Unit.SEASONS])
@pytest.mark.parametrize("gone", [False, True])
def test_missing_arr_item_is_not_an_error_but_still_needs_disk_verification(conn, tmp_path, unit, gone):
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    scenario.radarr.movies.clear()
    scenario.sonarr.series.clear()
    if gone:
        for path in scenario.paths[1]:
            path.unlink()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert report.outcomes[0].status == ("deleted" if gone else "failed")
    assert scenario.row(request)["state"] == ("deleted" if gone else "removal_failed")
    assert report.outcomes[0].freed_bytes == 0


def test_retry_cancels_instead_of_deleting_if_a_user_started_watching(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.radarr.fail_always("PUT /api/v3/movie/1", 500)
    with scenario.executor() as (executor, radarr, _):
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0].status == "failed"
        user = seed_user(conn, "1", "Marc")
        seed_play(conn, user, "item-1", scenario.clock())
        scenario.radarr.always_fail.clear()
        report = scenario.recheck()
        assert len(report.cancelled) == 1
        count = len(radarr.requests)
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
        assert len(radarr.requests) == count
    assert scenario.row(request)["state"] == "cancelled"
    assert scenario.paths[1][0].exists()


@pytest.mark.parametrize("links", ["ordinary", "outside", "two_inside", "duplicate_path"])
def test_freed_space_counts_distinct_inodes_only_when_all_links_are_gone(conn, tmp_path, links):
    unit = Unit.SERIES if links in ("two_inside", "duplicate_path") else Unit.MOVIE
    scenario = Scenario(conn, tmp_path, unit=unit)
    scenario.add()
    first = scenario.paths[1][0]
    if links == "outside":
        os.link(first, tmp_path / "torrent-copy.mkv")
    elif links == "two_inside":
        second = scenario.paths[1][1]
        second.unlink()
        os.link(first, second)
    elif links == "duplicate_path":
        conn.execute("UPDATE episodes SET file_path = ?", (str(first),))
        conn.commit()
    with scenario.executor() as (executor, _, __):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
    assert outcome.status == "deleted"
    assert outcome.size_bytes == 17
    assert outcome.freed_bytes == (0 if links == "outside" else 17)
    if links == "outside":
        assert (tmp_path / "torrent-copy.mkv").read_bytes() == b"x" * 17


def test_history_is_durable_before_collection_cleanup_and_success_log(conn, tmp_path, caplog):
    from almali_library_curator.hostfs import HostFilesystem

    caplog.set_level(logging.INFO)
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    request = scenario.add()
    disk = tmp_path / "mnt" / "disk3"
    disk.mkdir(parents=True)
    (disk / scenario.paths[1][0].name).write_bytes(b"disk attribution")
    hostfs = HostFilesystem(unraid_root=tmp_path / "mnt", media_root=tmp_path)
    observed = []
    original = scenario.collections.remove_from_collection

    def remove(collection_key, rating_key):
        event = conn.execute("SELECT * FROM removal_events WHERE outcome = 'deleted'").fetchone()
        observed.append((scenario.row(request)["state"], event is not None))
        original(collection_key, rating_key)

    scenario.collections.remove_from_collection = remove
    with scenario.executor(hostfs=hostfs) as (executor, _, __):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
    event = conn.execute("SELECT * FROM removal_events").fetchone()
    assert event is not None
    assert observed == [("deleted", True)]
    assert event["outcome"] == "deleted"
    assert event["title"] == "Title 1 S1"
    assert event["unit"] == "seasons"
    assert event["category"] == request.category.value
    assert event["reason"] == "never_watched example reason"
    assert event["size_bytes"] == event["freed_bytes"] == 17
    assert event["disk"] == "disk3"
    assert event["marked_by"] == "owner"
    assert event["occurred_at"] == scenario.clock().isoformat()
    assert outcome.request.deleted_at == scenario.clock()
    assert outcome.request.last_error is None
    assert not outcome.request.collection_members
    records = [r for r in caplog.records if r.message == "removal_deleted"]
    assert len(records) == 1 and records[0].freed_bytes == 17
    assert len(conn.execute("SELECT * FROM removal_events").fetchall()) == 1


def test_failed_event_records_error_without_secrets_and_retry_records_success(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.radarr.on_delete_movie = lambda identifier: None
    with scenario.executor() as (executor, _, __):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        event = conn.execute("SELECT * FROM removal_events").fetchone()
        assert event is not None
        assert event["outcome"] == "failed"
        assert event["error"] == outcome.error == scenario.row(request)["last_error"]
        assert event["freed_bytes"] == 0
        assert scenario.radarr.api_key not in str(dict(event))
        assert scenario.collections.holds("item-1")
        scenario.paths[1][0].unlink()
        assert len(scenario.recheck().passed) == 1
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0].status == "deleted"
    assert [r["outcome"] for r in conn.execute("SELECT * FROM removal_events ORDER BY id")] == ["failed", "deleted"]
    assert scenario.row(request)["last_error"] is None


def test_history_joins_distinct_disks(conn, tmp_path):
    from almali_library_curator.hostfs import HostFilesystem

    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    scenario.add()
    for number, path in enumerate(scenario.paths[1], 1):
        disk = tmp_path / "mnt" / f"disk{number}"
        disk.mkdir(parents=True)
        (disk / path.name).write_bytes(b"disk attribution")
    with scenario.executor(hostfs=HostFilesystem(tmp_path / "mnt", tmp_path)) as (executor, _, __):
        executor.run_deletions(passed_ids=scenario.passed_ids)
    event = conn.execute("SELECT * FROM removal_events").fetchone()
    assert event["disk"] == "disk1, disk2"
    assert event["size_bytes"] == event["freed_bytes"] == 34


def test_each_scope_file_is_statted_once_before_arr_calls(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    calls = []
    observed = []

    def stat(path):
        calls.append(str(path))
        return os.stat(path)

    original = scenario.radarr.on_delete_movie

    def delete(identifier):
        observed.append(list(calls))
        original(identifier)

    scenario.radarr.on_delete_movie = delete
    with scenario.executor(stat=stat) as (executor, _, __):
        executor.run_deletions(passed_ids=scenario.passed_ids)
    assert observed == [[str(scenario.paths[1][0])]]
    assert calls == [str(scenario.paths[1][0])] * 2


@pytest.mark.parametrize("endpoint", ["GET /api/v3/episodefile", "DELETE /api/v3/episodefile/bulk"])
def test_season_endpoint_errors_cannot_report_success(conn, tmp_path, endpoint):
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    request = scenario.add()
    scenario.sonarr.fail_always(endpoint, 404)
    with scenario.executor() as (executor, _, __):
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0].status == "failed"
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.paths[1][0].is_file()
    assert scenario.sonarr.import_exclusions == []


def test_a_404_from_import_exclusion_is_not_mistaken_for_an_absent_item(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.radarr.fail_always("POST /api/v3/importlistexclusion", 404)
    with scenario.executor() as (executor, radarr, _):
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0].status == "failed"
        assert not any(r["method"] == "DELETE" for r in radarr.requests)
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.paths[1][0].is_file()


def test_deleted_cleanup_can_retry_without_repeating_arr_or_history(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.collections.fail_remove.add("item-1")
    with scenario.executor() as (executor, radarr, _):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert outcome.status == "deleted"
        assert outcome.request.collection_members == ("item-1",)
        count = len(radarr.requests)
        scenario.recheck()
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
        assert len(radarr.requests) == count
    assert not scenario.collections.holds("item-1")
    assert scenario.row(request)["collection_added"] == 0
    assert conn.execute("SELECT count(*) FROM removal_events").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["undo", "keep_it"])
def test_cancellation_during_arr_execution_is_refused_and_deletion_is_recorded(conn, tmp_path, action):
    from almali_library_curator.db import connect

    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    database = conn.execute("PRAGMA database_list").fetchone()["file"]
    decisions = []
    states = []
    original = scenario.radarr.on_delete_movie

    def delete(identifier):
        # The HTTP handler runs on another thread: a second connection also
        # proves the deletion claim was committed before the arr operation.
        other = connect(database)
        try:
            engine = LifecycleEngine(other, settings=scenario.settings, clock=scenario.clock)
            states.append(engine.open_requests()[0].state.value)
            try:
                getattr(engine, action)(request)
            except RequestStateError:
                decisions.append("refused")
            else:
                decisions.append("accepted")
        finally:
            other.close()
        original(identifier)

    scenario.radarr.on_delete_movie = delete
    with scenario.executor() as (executor, _, __):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
    assert decisions == ["refused"]
    assert states == ["deleting"]
    assert outcome.status == "deleted"
    assert scenario.row(request)["state"] == "deleted"
    assert scenario.row(request)["deleted_at"] is not None
    assert [r["outcome"] for r in conn.execute("SELECT * FROM removal_events")] == ["deleted"]


def test_unknown_pre_stat_cannot_claim_any_freed_bytes(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    calls = 0

    def stat(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("unreadable before deletion")
        return os.stat(path)

    with scenario.executor(stat=stat) as (executor, _, __):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
    assert outcome.status == "deleted"
    assert outcome.freed_bytes == 0


def test_a_request_cancelled_after_batch_selection_is_never_sent_to_arr(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add(1)
    second = scenario.add(2)
    cancelled = False

    def stat(path):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            scenario.engine.undo(second)
        return os.stat(path)

    with scenario.executor(stat=stat) as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert len(report.outcomes) == 1
    assert scenario.row(second)["state"] == "cancelled"
    assert scenario.paths[2][0].exists()
    assert not any(r["path"] == "/api/v3/movie/2" for r in radarr.requests)


def test_a_request_cancelled_during_preflight_is_not_sent_to_arr(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    cancelled = False

    def stat(path):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            scenario.engine.undo(request)
        return os.stat(path)

    with scenario.executor(stat=stat) as (executor, radarr, _):
        executor.run_deletions(passed_ids=scenario.passed_ids)
        assert radarr.requests == []
    assert scenario.paths[1][0].exists()
    assert scenario.row(request)["state"] == "cancelled"
    assert [r["outcome"] for r in conn.execute("SELECT * FROM removal_events")] == ["cancelled"]


def test_an_unreadable_request_does_not_starve_other_deletions(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1)
    second = scenario.add(2)
    conn.execute("UPDATE candidates SET facts = 'damaged' WHERE id = ?", (first.candidate_id,))
    conn.commit()
    with scenario.executor() as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [o.request_id for o in report.outcomes] == [first.id, second.id]
    assert [o.status for o in report.outcomes] == ["failed", "deleted"]
    assert scenario.paths[1][0].exists()
    assert not any(r["path"] == "/api/v3/movie/1" for r in radarr.requests)


def test_interrupted_deletion_fails_and_requires_a_new_final_check(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    conn.execute("UPDATE removal_requests SET state = 'deleting' WHERE id = ?", (request.id,))
    conn.commit()
    with scenario.executor() as (executor, radarr, _):
        executor.run_deletions(passed_ids=scenario.passed_ids)
        assert scenario.row(request)["state"] == "removal_failed"
        # S16 rule 3: the stored failure is the translation key, so the
        # reader's language decides the wording.
        expected = "removal.failed.interrupted"
        assert scenario.row(request)["last_error"] == expected
        assert radarr.requests == []
        event = conn.execute("SELECT * FROM removal_events").fetchone()
        assert event["outcome"] == "failed"
        assert event["error"] == expected
        assert event["freed_bytes"] == 0
        assert scenario.collections.holds("item-1")
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
        assert len(scenario.recheck().passed) == 1
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0].status == "deleted"
    assert [r["outcome"] for r in conn.execute("SELECT * FROM removal_events")] == ["failed", "deleted"]


def test_deleting_members_are_repaired_and_are_not_rechecked_or_cleaned_up(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    conn.execute("UPDATE removal_requests SET state = 'deleting', collection_added = 0 WHERE id = ?", (request.id,))
    conn.execute("UPDATE candidates SET facts = json_set(facts, '$.collection_members', json('[]')) WHERE id = ?",
                 (request.candidate_id,))
    conn.commit()
    scenario.collections.membership.clear()
    assert scenario.engine.retry_collection_additions() == 1
    assert scenario.collections.holds("item-1")
    assert scenario.engine.retry_collection_cleanup() == 0
    assert scenario.engine.run_recheck().outcomes == ()
    assert scenario.engine.open_requests()[0].state.value == "deleting"


def test_dry_run_does_not_recover_interrupted_deletions(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, dry_run=True)
    scenario.add()
    conn.execute("UPDATE removal_requests SET state = 'deleting'")
    conn.commit()
    before = list(conn.iterdump())
    with scenario.executor() as (executor, radarr, sonarr):
        assert executor.run_deletions(passed_ids=scenario.passed_ids).outcomes == ()
        assert radarr.requests == sonarr.requests == []
    assert list(conn.iterdump()) == before


def test_dry_run_reports_an_unreadable_request_as_failed_without_mutation(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, dry_run=True)
    request = scenario.add()
    conn.execute("UPDATE candidates SET facts = 'damaged' WHERE id = ?", (request.candidate_id,))
    conn.commit()
    before = list(conn.iterdump())
    with scenario.executor() as (executor, radarr, sonarr):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert outcome.status == "failed"
        assert outcome.error
        assert radarr.requests == sonarr.requests == []
    assert list(conn.iterdump()) == before


def test_dry_run_changes_nothing_and_only_reports_passed_requests(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, dry_run=True)
    request = scenario.add(1)
    scenario.add(2, pass_check=False)
    before = list(conn.iterdump())
    with scenario.executor() as (executor, radarr, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert [(o.request.id, o.status) for o in report.outcomes] == [(request.id, "would_delete")]
        assert radarr.requests == sonarr.requests == []
    assert list(conn.iterdump()) == before
    assert all(path.exists() for paths in scenario.paths.values() for path in paths)
