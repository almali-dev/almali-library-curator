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
    def __init__(self, conn, tmp_path, *, unit=Unit.MOVIE, dry_run=False,
                 media_root=None):
        self.conn = conn
        self.root = tmp_path
        self.clock = FrozenClock()
        self.collections = FakeCollections()
        # The arr fakes report host-side /data paths; the Plex path map maps
        # them onto the container's media root (tmp_path), which the app can
        # read, so ownership of every marked file is provable.
        self.settings = default_settings(
            dry_run=dry_run,
            plex_path_map=(("/data", str(tmp_path)),),
            media_root=str(tmp_path) if media_root is None else media_root,
        )
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
            self.conn.execute(
                "UPDATE media_items SET file_path = ?, radarr_id = ?, tmdb_id = ? WHERE id = ?",
                (str(path), identifier, 2000 + identifier, item))
            self.radarr.movies.append(
                movie_resource(identifier, title, path="/data"))
        else:
            self.conn.execute(
                "UPDATE media_items SET sonarr_id = ?, tvdb_id = ? WHERE id = ?",
                (identifier, 1000 + identifier, item))
            self.sonarr.series.append(
                series_resource(identifier, title, path="/data"))
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
                self.sonarr.episode_files.append({
                    "id": file_id, "seriesId": identifier, "seasonNumber": season,
                    "path": f"/data/{identifier}-s{season}.mkv",
                })
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

    @contextmanager
    def dry_executor(self):
        """A preview executor over the same database as this real scenario.

        The preview must reach the same outcome as the real run without
        mutating anything, so both engines share one database and clock.
        """
        from almali_library_curator.lifecycle.deletion import DeletionExecutor

        engine = LifecycleEngine(
            self.conn,
            settings=default_settings(
                dry_run=True,
                plex_path_map=(("/data", str(self.root)),),
                media_root=str(self.root),
            ),
            clock=self.clock, collections=self.collections,
        )
        with FakeRadarrServer(self.radarr) as radarr, FakeSonarrServer(self.sonarr) as sonarr:
            yield DeletionExecutor(
                engine,
                radarr=RadarrClient(radarr.base_url, self.radarr.api_key,
                                    rate_limit_per_second=0, max_retries=0),
                sonarr=SonarrClient(sonarr.base_url, self.sonarr.api_key,
                                    rate_limit_per_second=0, max_retries=0),
            ), radarr, sonarr

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
def test_a_missing_arr_resource_is_an_identity_failure_and_sends_nothing(conn, tmp_path, unit):
    # #39: a missing arr resource must never return silently and let the
    # request become "deleted"; #38: nothing is mutated when ownership
    # cannot be confirmed by re-reading the resource.
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    scenario.radarr.movies.clear()
    scenario.sonarr.series.clear()
    with scenario.executor() as (executor, radarr, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        service = "radarr" if unit is Unit.MOVIE else "sonarr"
        server = radarr if unit is Unit.MOVIE else sonarr
        assert report.outcomes[0].status == "failed"
        assert report.outcomes[0].error == f"removal.failed.{service}_identity"
        assert not any(r["method"] != "GET" for r in server.requests)
    assert scenario.row(request)["state"] == "removal_failed"
    assert all(path.exists() for path in scenario.paths[1])


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_arr_delete_refuses_when_the_resource_path_does_not_own_the_marked_file(conn, tmp_path, unit):
    # #38 scenario A: the resource Radarr/Sonarr would delete lives on
    # another path than the marked files, so the target does not own them.
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    if scenario.radarr.movies:
        scenario.radarr.movies[0]["path"] = "/elsewhere"
    if scenario.sonarr.series:
        scenario.sonarr.series[0]["path"] = "/elsewhere"
    with scenario.executor() as (executor, radarr, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        service = "radarr" if unit is Unit.MOVIE else "sonarr"
        server = radarr if unit is Unit.MOVIE else sonarr
        assert report.outcomes[0].error == f"removal.failed.{service}_identity"
        assert not any(r["method"] != "GET" for r in server.requests)
    assert all(path.exists() for path in scenario.paths[1])
    assert scenario.row(request)["state"] == "removal_failed"


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_arr_delete_refuses_when_the_external_id_does_not_match_the_plex_item(conn, tmp_path, unit):
    # #38 scenario C: the stored arr id now names another movie or series,
    # which the external id mismatch proves before anything is sent.
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    if scenario.radarr.movies:
        scenario.radarr.movies[0]["tmdbId"] = 999999
    if scenario.sonarr.series:
        scenario.sonarr.series[0]["tvdbId"] = 999999
    with scenario.executor() as (executor, radarr, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        service = "radarr" if unit is Unit.MOVIE else "sonarr"
        server = radarr if unit is Unit.MOVIE else sonarr
        assert report.outcomes[0].error == f"removal.failed.{service}_identity"
        assert not any(r["method"] != "GET" for r in server.requests)
    assert all(path.exists() for path in scenario.paths[1])
    assert scenario.row(request)["state"] == "removal_failed"


def test_season_delete_deletes_only_episode_files_whose_mapped_path_is_in_the_scope(conn, tmp_path):
    # #38: the episode files actually deleted are the ones whose mapped
    # path belongs to the marked scope, and only those.
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add()
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [o.status for o in report.outcomes] == ["deleted"]
    bulk = sonarr.requests_for("/api/v3/episodefile/bulk")[-1]
    assert bulk["body"]["episodeFileIds"] == [11]
    assert not scenario.paths[1][0].exists()
    assert scenario.paths[1][1].exists()


def test_season_delete_refuses_when_the_arr_season_holds_files_outside_the_scope(conn, tmp_path):
    # #38 scenario B: Sonarr's season holds a file the marked scope does
    # not, so deleting the season would remove more than the owner marked.
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    request = scenario.add()
    scenario.sonarr.episode_files.append(
        {"id": 99, "seriesId": 1, "seasonNumber": 1, "path": "/data/extra.mkv"})
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert report.outcomes[0].error == "removal.failed.sonarr_identity"
        assert not any(r["method"] != "GET" for r in sonarr.requests)
    assert all(path.exists() for path in scenario.paths[1])
    assert scenario.row(request)["state"] == "removal_failed"


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_arr_delete_owns_the_marked_files_when_no_external_id_is_stored(conn, tmp_path, unit):
    # #38 MUST: linking by exact title+year (or a legacy agent guid) stores
    # no external id, so ownership must stand on the mapped resource path.
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    column = "tmdb_id" if unit is Unit.MOVIE else "tvdb_id"
    conn.execute(f"UPDATE media_items SET {column} = NULL")
    conn.commit()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert report.outcomes[0].status == "deleted"
    assert not scenario.paths[1][0].exists()
    assert scenario.row(request)["state"] == "deleted"


def test_files_outside_the_configured_media_root_are_not_refused_by_the_root_gate(conn, tmp_path):
    # #39 MUST: the media-root gate only bites when the marked files are
    # expected under that root; a deployment mounted elsewhere must not be
    # refused by it. The per-file pre-stat remains the real guard.
    scenario = Scenario(conn, tmp_path, media_root=str(tmp_path / "not-mounted"))
    request = scenario.add()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert report.outcomes[0].status == "deleted"
    assert scenario.row(request)["state"] == "deleted"


def test_an_empty_media_root_never_gates_a_deletion(conn, tmp_path):
    # #39: without a configured media root there is nothing to gate on.
    scenario = Scenario(conn, tmp_path, media_root="")
    request = scenario.add()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert report.outcomes[0].status == "deleted"
    assert scenario.row(request)["state"] == "deleted"


def test_a_first_attempt_with_a_never_seen_file_fails_and_sends_nothing(conn, tmp_path):
    # #39 MUST (strict per file): one file of the scope was never on disk,
    # so a sibling's observation must not authorise the whole scope. The
    # environment is not what the app believes, and it refuses to act.
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    request = scenario.add()
    scenario.paths[1][0].unlink()  # never seen by any attempt
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert report.outcomes[0].status == "failed"
        assert report.outcomes[0].error == "removal.failed.files_unknown"
        assert sonarr.requests == []
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.row(request)["deleted_at"] is None
    assert scenario.paths[1][1].is_file()


def test_dry_run_refuses_an_arr_target_the_real_run_would_refuse(conn, tmp_path):
    # #38 MUST: the preview reaches the same ownership refusal as the real
    # run, sending no mutating request at all.
    scenario = Scenario(conn, tmp_path, dry_run=True)
    scenario.add()
    scenario.radarr.movies[0]["path"] = "/elsewhere"
    before = list(conn.iterdump())
    with scenario.executor() as (executor, radarr, _):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert outcome.status == "failed"
        assert outcome.error == "removal.failed.radarr_identity"
        assert not any(r["method"] != "GET" for r in radarr.requests)
    assert list(conn.iterdump()) == before
    assert scenario.paths[1][0].is_file()


def test_the_preview_reports_files_present_when_nothing_would_be_sent(conn, tmp_path):
    # MUST 1: an earlier attempt recorded the evidence, the Radarr
    # resource has since vanished and the files are still on disk, so
    # the real run sends nothing and records files_present. The preview
    # must reach that same outcome instead of promising a deletion it
    # refuses to perform.
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    scenario.radarr.fail_always("POST /api/v3/importlistexclusion", 500)
    with scenario.executor() as (executor, _, __):
        first = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert first.status == "failed"
        scenario.radarr.movies.clear()
        scenario.radarr.always_fail.clear()
    scenario.recheck()
    with scenario.dry_executor() as (executor, radarr, _):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert outcome.status == "failed"
        assert outcome.error == "removal.failed.files_present"
        assert not any(r["method"] != "GET" for r in radarr.requests)
    # The preview matched the real run: nothing is sent, the disk decides.
    with scenario.executor() as (executor, radarr, _):
        real = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert real.status == "failed"
        assert real.error == "removal.failed.files_present"
        assert not any(r["method"] != "GET" for r in radarr.requests)


def test_an_unrecordable_evidence_table_never_aborts_the_batch(conn, tmp_path):
    # MUST: only a missing evidence table is created on demand; any other
    # operational error is logged and the batch continues, so no request
    # is left stuck in "deleting".
    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1)
    second = scenario.add(2)
    # A view over the evidence table's name makes every evidence insert an
    # operational error that is not "no such table".
    conn.execute(
        "CREATE VIEW removal_seen_files AS SELECT 1 AS removal_request_id,"
        " 'x' AS file_path, 'y' AS seen_at")
    conn.commit()
    scenario.radarr.fail_always("PUT /api/v3/movie/1", 500)
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert [o.status for o in report.outcomes] == ["failed", "deleted"]
    assert scenario.row(first)["state"] == "removal_failed"
    assert scenario.row(second)["state"] == "deleted"


def test_a_failing_evidence_commit_never_strands_a_request_in_deleting(conn, tmp_path):
    # MUST 2: the evidence commit must sit inside its own guard. A
    # recording failure inside the try is absorbed by the outer except,
    # and the same failure inside that error handler must not escape
    # run_deletions and leave the request stranded in "deleting".
    from almali_library_curator.lifecycle.deletion import DeletionExecutor

    scenario = Scenario(conn, tmp_path)
    first = scenario.add(1)
    second = scenario.add(2)
    conn.execute(DeletionExecutor._SEEN_FILES_TABLE)
    conn.execute(
        "CREATE TRIGGER refuse_evidence BEFORE INSERT ON removal_seen_files"
        " BEGIN SELECT RAISE(ABORT, 'evidence refused'); END")
    conn.commit()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert [o.status for o in report.outcomes] == ["deleted", "deleted"]
    assert scenario.row(first)["state"] == "deleted"
    assert scenario.row(second)["state"] == "deleted"
    stranded = conn.execute(
        "SELECT COUNT(*) FROM removal_requests WHERE state = 'deleting'"
    ).fetchone()[0]
    assert stranded == 0


def test_a_resource_path_of_slash_owns_nothing(conn, tmp_path):
    # #38 MUST: "/" must not map to "every absolute path"; an empty or
    # root resource path is a fail-open the ownership proof must close.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.radarr.movies[0]["path"] = "/"
    with scenario.executor() as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert report.outcomes[0].error == "removal.failed.radarr_identity"
        assert not any(r["method"] != "GET" for r in radarr.requests)
    assert scenario.paths[1][0].is_file()
    assert scenario.row(request)["state"] == "removal_failed"


def test_files_never_seen_on_disk_cannot_be_reported_deleted(conn, tmp_path):
    # #39: no file of the scope was ever seen, so "deleted" can never be
    # claimed and nothing is sent to the arr at all.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    for path in scenario.paths[1]:
        path.unlink()
    with scenario.executor() as (executor, radarr, _):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert report.outcomes[0].status == "failed"
        assert report.outcomes[0].error == "removal.failed.files_unknown"
        assert radarr.requests == []
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.row(request)["deleted_at"] is None


def test_an_unreadable_media_root_fails_with_files_unknown_without_any_request(conn, tmp_path):
    # #39 MUST: the marked files live under the configured media root and
    # that root cannot be read (traversable but not readable), so the gate
    # refuses before any request is sent. A plain stat would still succeed
    # here, so the gate is what fires.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    tmp_path.chmod(0o300)
    try:
        with scenario.executor() as (executor, radarr, _):
            report = executor.run_deletions(passed_ids=scenario.passed_ids)
            assert report.outcomes[0].status == "failed"
            assert report.outcomes[0].error == "removal.failed.files_unknown"
            assert radarr.requests == []
    finally:
        tmp_path.chmod(0o700)
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.paths[1][0].is_file()


def test_already_gone_requires_every_scope_file_seen_in_an_earlier_attempt(conn, tmp_path):
    # #39 MUST: the durable "seen earlier" proof is per file. One file of
    # the scope observed once must not authorise "already gone" for the
    # whole scope, and zero-byte files are no exception.
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    request = scenario.add()
    scenario.paths[1][0].unlink()  # only one file was ever seen
    scenario.sonarr.fail_always("DELETE /api/v3/series/1", 500)
    with scenario.executor() as (executor, _, sonarr):
        first = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert first.status == "failed"
        scenario.paths[1][1].unlink()
        scenario.sonarr.always_fail.clear()
        scenario.recheck()
        requests_before = len(sonarr.requests)
        retry = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert retry.status == "failed"
        assert retry.error == "removal.failed.files_unknown"
        assert len(sonarr.requests) == requests_before  # nothing was sent
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.row(request)["deleted_at"] is None


def test_a_missing_arr_resource_on_retry_claims_only_what_was_seen(conn, tmp_path):
    # #38/#39: a resource a durable earlier attempt really saw may be gone
    # on retry; the disk verification then decides the outcome.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.radarr.fail_always("POST /api/v3/importlistexclusion", 500)
    with scenario.executor() as (executor, _, __):
        first = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert first.status == "failed"
        scenario.radarr.movies.clear()
        scenario.paths[1][0].unlink()
        scenario.radarr.always_fail.clear()
        scenario.recheck()
        retry = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert retry.status == "deleted"
    assert scenario.row(request)["state"] == "deleted"


def test_a_per_movie_folder_layout_proves_ownership(conn, tmp_path):
    # #38: the resource path is the movie's own folder, not the media root.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    folder = tmp_path / "Title 1"
    folder.mkdir()
    moved = folder / "Title 1.mkv"
    scenario.paths[1][0].rename(moved)
    scenario.paths[1][0] = moved
    conn.execute("UPDATE media_items SET file_path = ?", (str(moved),))
    scenario.radarr.movies[0]["path"] = "/data/Title 1"
    conn.commit()
    with scenario.executor() as (executor, _, __):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert report.outcomes[0].status == "deleted"
    assert not moved.exists()
    assert scenario.row(request)["state"] == "deleted"


def test_dry_run_applies_the_same_preflight_rules_as_the_real_run(conn, tmp_path):
    # #39 SHOULD: the preview must not promise a deletion the real run
    # would refuse before sending anything.
    scenario = Scenario(conn, tmp_path, dry_run=True)
    scenario.add()
    tmp_path.chmod(0o300)
    try:
        with scenario.executor() as (executor, radarr, sonarr):
            outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
            assert outcome.status == "failed"
            assert outcome.error == "removal.failed.files_unknown"
            assert radarr.requests == sonarr.requests == []
    finally:
        tmp_path.chmod(0o700)


def test_already_gone_is_allowed_only_on_a_retry_that_saw_the_files_earlier(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    # The first attempt sees the files but fails after the unmonitor call.
    scenario.radarr.fail_always("POST /api/v3/importlistexclusion", 500)
    with scenario.executor() as (executor, _, __):
        first = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert first.status == "failed"
        scenario.paths[1][0].unlink()
        scenario.radarr.always_fail.clear()
        scenario.recheck()
        retry = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert retry.status == "deleted"
    assert scenario.row(request)["state"] == "deleted"


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


def test_a_failed_pre_stat_fails_with_files_unknown_without_any_request(conn, tmp_path):
    # #39: a failed pre-delete stat is never ignored; the request fails
    # before any arr or Plex call is made.
    scenario = Scenario(conn, tmp_path)
    scenario.add()

    def stat(path):
        raise PermissionError("unreadable before deletion")

    with scenario.executor(stat=stat) as (executor, radarr, _):
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]
        assert outcome.status == "failed"
        assert outcome.error == "removal.failed.files_unknown"
        assert radarr.requests == []
    assert scenario.paths[1][0].is_file()


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
        # The preview proves ownership with read-only requests and sends
        # no mutation at all.
        assert not any(r["method"] != "GET" for server in (radarr, sonarr) for r in server.requests)
    assert list(conn.iterdump()) == before
    assert all(path.exists() for paths in scenario.paths.values() for path in paths)
