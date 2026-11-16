"""S3 (#69) — a cancelled failed removal undoes the arr side effects it applied.

A removal that fails after the arr was mutated leaves the title unmonitored
and on the import exclusion list. The executor records every side effect it
applies, and every cancellation — the owner's Undo and Keep it, and the
final check's own exits — reverts them through the arr clients before the
cancellation becomes durable.

The failures here are produced by the real executor against the fake arr
servers, so the record under test is the one a real removal writes, and the
revert is exercised over the same clients a deployment uses. A revert that
fails is reported and leaves the request exactly where it was, still
carrying the effects that are still applied.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.db import connect
from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import (
    AppliedEffect,
    LifecycleEngine,
    LifecycleError,
    RemovalState,
    RequestStateError,
    SideEffectRevertError,
)
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from almali_library_curator.lifecycle.reverts import ArrReverter
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from lifecycle_helpers import (
    FakeCollections,
    FrozenClock,
    default_settings,
    make_candidate,
    seed_episode,
    seed_item,
    seed_library,
    seed_play,
    seed_user,
)

# The step whose injected failure leaves the side effects applied: the
# unmonitor and the exclusion already ran, the delete never did.
DELETE_STEP = {
    Unit.MOVIE: "DELETE /api/v3/movie/1",
    Unit.SERIES: "DELETE /api/v3/series/1",
    Unit.SEASONS: "DELETE /api/v3/episodefile/bulk",
}


def row(conn, request):
    return conn.execute(
        "SELECT * FROM removal_requests WHERE id = ?", (request.id,)
    ).fetchone()


def recorded_effects(conn, request):
    return json.loads(row(conn, request)["applied_effects"] or "[]")


class Removal:
    """One real removal over the fake arr servers, marked and failed or deleted."""

    def __init__(self, conn, tmp_path, unit=Unit.MOVIE, seasons=(1,)):
        self.conn = conn
        self.root = tmp_path
        self.unit = unit
        self.seasons = seasons
        self.clock = FrozenClock()
        self.collections = FakeCollections()
        # The arr fakes report host-side /data paths; the Plex path map maps
        # them under tmp_path, which the app can read, so ownership of every
        # marked file is provable exactly as in a deployment.
        self.settings = default_settings(
            plex_path_map=(("/data", str(tmp_path)),), media_root=str(tmp_path)
        )
        self.radarr_data = FakeRadarrDataset()
        self.sonarr_data = FakeSonarrDataset()
        # A real arr removes the files it deletes; the fake does it through
        # these hooks, wired exactly as the deletion scenarios wire them.
        self.radarr_data.on_delete_movie = lambda identifier: self._unlink_paths()
        self.sonarr_data.on_delete_series = lambda identifier: self._unlink_paths()
        self.sonarr_data.on_delete_episode_files = self._unlink_episode_files
        self.library = seed_library(conn, "1", "Library")
        self.paths = []
        self.episode_file_paths = {}
        self.request = None
        self.passed = []

    # -- seeding ----------------------------------------------------------

    def _seed_movie(self):
        item = seed_item(self.conn, self.library, "item-1", "Title 1")
        path = self.root / "1.mkv"
        path.write_bytes(b"x" * 17)
        self.paths.append(path)
        self.conn.execute(
            "UPDATE media_items SET file_path = ?, radarr_id = 1, tmdb_id = 2001"
            " WHERE id = ?",
            (str(path), item),
        )
        self.radarr_data.movies.append(movie_resource(1, "Title 1", path="/data"))
        self.conn.commit()
        return item

    def _seed_series(self):
        item = seed_item(
            self.conn, self.library, "item-1", "Title 1", item_type="series"
        )
        self.conn.execute(
            "UPDATE media_items SET sonarr_id = 1, tvdb_id = 1001 WHERE id = ?",
            (item,),
        )
        self.sonarr_data.series.append(series_resource(1, "Title 1", path="/data"))
        for season in (1, 2):
            seed_item(
                self.conn, self.library, f"item-1-s{season}", f"Season {season}",
                item_type="season", parent_rating_key="item-1", season_number=season,
            )
            episode = seed_episode(self.conn, "item-1", season, 1)
            path = self.root / f"1-s{season}.mkv"
            path.write_bytes(b"x" * 17)
            self.paths.append(path)
            self.conn.execute(
                "UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                (str(path), episode),
            )
            self.sonarr_data.episode_files.append({
                "id": 10 + season, "seriesId": 1, "seasonNumber": season,
                "path": f"/data/1-s{season}.mkv",
            })
            self.episode_file_paths[10 + season] = path
        self.conn.commit()
        return item

    def _unlink_paths(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)

    def _unlink_episode_files(self, identifiers) -> None:
        for identifier, path in self.episode_file_paths.items():
            if identifier in identifiers:
                path.unlink(missing_ok=True)

    # -- the removal ------------------------------------------------------

    @contextmanager
    def attached(self):
        with FakeRadarrServer(self.radarr_data) as radarr, FakeSonarrServer(
            self.sonarr_data
        ) as sonarr:
            self.radarr = RadarrClient(
                radarr.base_url, self.radarr_data.api_key,
                rate_limit_per_second=0, max_retries=0,
            )
            self.sonarr = SonarrClient(
                sonarr.base_url, self.sonarr_data.api_key,
                rate_limit_per_second=0, max_retries=0,
            )
            self.radarr_server, self.sonarr_server = radarr, sonarr
            self.engine = LifecycleEngine(
                self.conn, settings=self.settings, clock=self.clock,
                collections=self.collections,
                reverter=ArrReverter(radarr=self.radarr, sonarr=self.sonarr),
            )
            self.executor = DeletionExecutor(
                self.engine, radarr=self.radarr, sonarr=self.sonarr
            )
            yield self

    def mark(self):
        item = self._seed_movie() if self.unit is Unit.MOVIE else self._seed_series()
        candidate = make_candidate(
            item, "item-1", "Title 1", unit=self.unit,
            seasons=self.seasons if self.unit is Unit.SEASONS else (),
        )
        self.request = self.engine.mark_for_removal(candidate, marked_by="owner")
        self.clock.advance(days=14)
        self.passed = [outcome.request.id for outcome in self.engine.run_recheck().passed]
        assert self.passed == [self.request.id]
        return self.request

    def fail_the_removal(self):
        """Inject a failure after the side effects, so they stay applied."""
        dataset = (
            self.radarr_data if self.unit is Unit.MOVIE else self.sonarr_data
        )
        dataset.fail_always(DELETE_STEP[self.unit], 400)
        outcome = self.executor.run_deletions(passed_ids=self.passed).outcomes[0]
        assert outcome.status == "failed"
        return self.open_request()

    def delete_the_removal(self):
        dataset = (
            self.radarr_data if self.unit is Unit.MOVIE else self.sonarr_data
        )
        dataset.always_fail.clear()
        self.passed = [
            outcome.request.id for outcome in self.engine.run_recheck().passed
        ]
        return self.executor.run_deletions(passed_ids=self.passed).outcomes[0]

    def open_request(self):
        return next(
            request
            for request in self.engine.open_requests()
            if request.id == self.request.id
        )

    def monitored(self):
        """Whether the arr still monitors the title (or the marked season)."""
        if self.unit is Unit.MOVIE:
            return self.radarr_data.movies[0]["monitored"]
        series = self.sonarr_data.series[0]
        if self.unit is Unit.SERIES:
            return series["monitored"]
        return [season["monitored"] for season in series["seasons"]]

    def exclusions(self):
        dataset = (
            self.radarr_data if self.unit is Unit.MOVIE else self.sonarr_data
        )
        return list(dataset.import_exclusions)


@pytest.fixture()
def removal(conn, tmp_path):
    return Removal(conn, tmp_path)


def make_removal(conn, tmp_path, unit=Unit.MOVIE, seasons=(1,)):
    return Removal(conn, tmp_path, unit=unit, seasons=seasons)


# -- what the executor records ------------------------------------------------


def test_a_removal_that_fails_after_the_exclusion_records_both_side_effects(
    conn, tmp_path
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()

        assert failed.state is RemovalState.REMOVAL_FAILED
        effects = recorded_effects(conn, request)
        assert [effect["kind"] for effect in effects] == ["unmonitor", "exclusion"]
        assert effects[0] == {"kind": "unmonitor", "arr": "radarr", "resource_id": 1}
        assert effects[1]["arr"] == "radarr"
        assert effects[1]["external_id"] == 2001
        assert effects[1]["exclusion_id"] == 1
        # The typed record the cancellation works from reads the same list.
        assert failed.applied_effects == (
            AppliedEffect(kind="unmonitor", arr="radarr", resource_id=1),
            AppliedEffect(
                kind="exclusion", arr="radarr", external_id=2001,
                exclusion_id=1, title="Title 1",
            ),
        )
        # The arr really is left unmonitored and excluded.
        assert case.monitored() is False
        assert case.exclusions() != []


def test_a_series_removal_records_the_series_unmonitor_and_exclusion(conn, tmp_path):
    with make_removal(conn, tmp_path, Unit.SERIES).attached() as case:
        request = case.mark()
        case.fail_the_removal()

        effects = recorded_effects(conn, request)
        assert effects[0] == {"kind": "unmonitor", "arr": "sonarr", "resource_id": 1}
        assert effects[1]["kind"] == "exclusion"
        assert effects[1]["arr"] == "sonarr"
        assert effects[1]["external_id"] == 1001
        assert case.monitored() is False
        assert case.exclusions() != []


def test_a_seasons_removal_records_the_unmonitored_seasons_and_no_exclusion(
    conn, tmp_path
):
    """A season scope never adds an import exclusion: the series stays."""
    with make_removal(conn, tmp_path, Unit.SEASONS).attached() as case:
        request = case.mark()
        case.fail_the_removal()

        effects = recorded_effects(conn, request)
        assert effects == [
            {"kind": "unmonitor", "arr": "sonarr", "resource_id": 1, "seasons": [1]}
        ]
        assert case.monitored() == [False, True]
        assert case.exclusions() == []


# -- the executor records only what it changed (#103) -------------------------


def test_a_cancel_keeps_a_movie_exclusion_the_owner_already_had(conn, tmp_path):
    """#103: the app reused this exclusion, so a cancel must not delete it."""
    with make_removal(conn, tmp_path).attached() as case:
        case.radarr_data.import_exclusions.append(
            {"id": 9, "tmdbId": 2001, "movieTitle": "Title 1", "movieYear": 2020}
        )
        request = case.mark()
        case.fail_the_removal()
        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "unmonitor"
        ]
        sent = len(case.radarr.requests)

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() is True
        assert [entry["id"] for entry in case.exclusions()] == [9]
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr.requests[sent:]
            if request_["method"] != "GET"
        ] == [("PUT", "/api/v3/movie/1")]


def test_a_cancel_keeps_a_series_exclusion_the_owner_already_had(conn, tmp_path):
    with make_removal(conn, tmp_path, Unit.SERIES).attached() as case:
        case.sonarr_data.import_exclusions.append(
            {"id": 9, "tvdbId": 1001, "title": "Title 1"}
        )
        request = case.mark()
        case.fail_the_removal()
        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "unmonitor"
        ]
        sent = len(case.sonarr.requests)

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert [entry["id"] for entry in case.exclusions()] == [9]
        assert [
            (request_["method"], request_["path"])
            for request_ in case.sonarr.requests[sent:]
            if request_["method"] != "GET"
        ] == [("PUT", "/api/v3/series/editor")]


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_a_cancel_does_not_monitor_a_title_the_owner_had_unmonitored(
    conn, tmp_path, unit
):
    """#103: an already-unmonitored title stays unmonitored after a cancel."""
    with make_removal(conn, tmp_path, unit).attached() as case:
        request = case.mark()
        dataset = case.radarr_data if unit is Unit.MOVIE else case.sonarr_data
        resources = dataset.movies if unit is Unit.MOVIE else dataset.series
        resources[0]["monitored"] = False

        case.fail_the_removal()

        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "exclusion"
        ]
        client = case.radarr if unit is Unit.MOVIE else case.sonarr
        sent = len(client.requests)

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() is False
        assert [
            request_["method"]
            for request_ in client.requests[sent:]
            if request_["method"] != "GET"
        ] == ["DELETE"]


def test_a_cancel_does_not_monitor_a_season_the_owner_had_unmonitored(conn, tmp_path):
    """#103: an already-unmonitored season has nothing to revert."""
    with make_removal(conn, tmp_path, Unit.SEASONS).attached() as case:
        request = case.mark()
        case.sonarr_data.series[0]["seasons"][0]["monitored"] = False

        case.fail_the_removal()

        assert recorded_effects(conn, request) == []
        sent = len(case.sonarr.requests)

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() == [False, True]
        assert [
            request_ for request_ in case.sonarr.requests[sent:]
            if request_["method"] != "GET"
        ] == []


def test_a_cancel_monitors_again_only_the_seasons_the_removal_unmonitored(
    conn, tmp_path
):
    """#103: of two marked seasons, the one the owner had unmonitored stays so."""
    with make_removal(conn, tmp_path, Unit.SEASONS, seasons=(1, 2)).attached() as case:
        request = case.mark()
        case.sonarr_data.series[0]["seasons"][0]["monitored"] = False

        case.fail_the_removal()

        assert recorded_effects(conn, request) == [
            {"kind": "unmonitor", "arr": "sonarr", "resource_id": 1, "seasons": [2]}
        ]
        assert case.monitored() == [False, False]

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() == [False, True]


def test_a_cancel_still_removes_an_exclusion_an_earlier_attempt_created(
    conn, tmp_path
):
    """#103 retry case: the retry reuses the entry, the cancel still reverts it."""
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "unmonitor", "exclusion",
        ]
        # The retry meets the exclusion the first attempt stored and the
        # unmonitor it already applied, and fails at the delete again.
        case.radarr_data.always_fail.clear()
        case.radarr_data.fail_always(DELETE_STEP[Unit.MOVIE], 400)
        passed = [outcome.request.id for outcome in case.engine.run_recheck().passed]
        assert case.executor.run_deletions(passed_ids=passed).outcomes[0].status == "failed"
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr.requests
            if request_["path"] in ("/api/v3/movie/1", "/api/v3/exclusions")
            and request_["method"] != "GET"
        ].count(("POST", "/api/v3/exclusions")) == 1
        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "unmonitor", "exclusion",
        ]

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.exclusions() == []
        assert case.monitored() is True


def test_a_record_written_by_an_earlier_version_still_reverts(conn, tmp_path):
    """#103 keeps the stored format: rows written before it still revert."""
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        stored = [
            {"kind": "unmonitor", "arr": "radarr", "resource_id": 1},
            {
                "kind": "exclusion", "arr": "radarr", "external_id": 2001,
                "exclusion_id": 1, "title": "Title 1",
            },
        ]
        conn.execute(
            "UPDATE removal_requests SET applied_effects = ? WHERE id = ?",
            (json.dumps(stored), request.id),
        )
        conn.commit()

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() is True
        assert case.exclusions() == []


# -- the revert itself --------------------------------------------------------


@pytest.mark.parametrize("action", ["undo", "keep_it"])
def test_cancelling_reverts_the_movie_unmonitor_and_removes_the_exclusion(
    conn, tmp_path, action
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        sent = len(case.radarr.requests)

        cancelled = getattr(case.engine, action)(failed)

        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() is True
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr.requests[sent:]
            if request_["method"] != "GET"
        ] == [
            ("PUT", "/api/v3/movie/1"),
            ("DELETE", "/api/v3/exclusions/1"),
        ]
        # The fake server saw the same two calls the client sent.
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr_server.requests[sent:]
            if request_["method"] != "GET"
        ] == [
            ("PUT", "/api/v3/movie/1"),
            ("DELETE", "/api/v3/exclusions/1"),
        ]
        assert not case.collections.holds("item-1")
        assert conn.execute(
            "SELECT COUNT(*) FROM removal_events WHERE outcome = 'cancelled'"
        ).fetchone()[0] == 1


def test_cancelling_reverts_the_series_unmonitor_and_removes_the_exclusion(
    conn, tmp_path
):
    with make_removal(conn, tmp_path, Unit.SERIES).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        sent = len(case.sonarr.requests)

        cancelled = case.engine.undo(failed)

        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() is True
        # The restore writes only the monitoring flag (#102).
        assert [
            (request_["method"], request_["path"])
            for request_ in case.sonarr.requests[sent:]
            if request_["method"] != "GET"
        ] == [
            ("PUT", "/api/v3/series/editor"),
            ("DELETE", "/api/v3/importlistexclusion/1"),
        ]


def test_cancelling_restores_exactly_the_seasons_the_removal_unmonitored(
    conn, tmp_path
):
    with make_removal(conn, tmp_path, Unit.SEASONS).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        assert case.monitored() == [False, True]
        sent = len(case.sonarr.requests)

        cancelled = case.engine.undo(failed)

        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() == [True, True]
        # The season restore names only the seasons the removal stopped (#102).
        writes = [
            request_ for request_ in case.sonarr.requests[sent:]
            if request_["method"] != "GET"
        ]
        assert [(request_["method"], request_["path"]) for request_ in writes] == [
            ("POST", "/api/v3/seasonpass")
        ]
        assert case.sonarr_server.requests_for("/api/v3/seasonpass")[-1]["body"] == {
            "series": [{"id": 1, "seasons": [{"seasonNumber": 1, "monitored": True}]}]
        }


def test_the_final_check_cancel_also_reverts_what_the_failure_applied(
    conn, tmp_path
):
    """Every cancellation reverts, not only the owner's Undo and Keep it."""
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        user = seed_user(conn, "1", "Marc")
        seed_play(conn, user, "item-1", case.clock())
        sent = len(case.radarr.requests)

        report = case.engine.run_recheck()

        assert [outcome.exit_code.value for outcome in report.cancelled] == [
            "started_watching"
        ]
        assert row(conn, request)["state"] == "cancelled"
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() is True
        assert [
            request_["method"]
            for request_ in case.radarr.requests[sent:]
            if request_["method"] != "GET"
        ] == ["PUT", "DELETE"]


def test_a_successful_removal_leaves_nothing_to_revert(conn, tmp_path):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        assert recorded_effects(conn, request)

        outcome = case.delete_the_removal()

        assert outcome.status == "deleted"
        assert row(conn, request)["applied_effects"] is None
        sent = len(case.radarr.requests)
        with pytest.raises(RequestStateError):
            case.engine.undo(request)
        assert [
            request_["method"]
            for request_ in case.radarr.requests[sent:]
            if request_["method"] != "GET"
        ] == []


# -- a revert that cannot finish ---------------------------------------------


def test_a_revert_failure_is_reported_and_leaves_a_truthful_record(conn, tmp_path):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        # The arr refuses to restore monitoring, so that effect stays applied.
        case.radarr_data.fail_always("PUT /api/v3/movie/1", 400)

        with pytest.raises(SideEffectRevertError) as raised:
            case.engine.undo(failed)

        assert [effect.kind for effect in raised.value.effects] == ["unmonitor"]
        # The cancellation did not happen: the request is still failed, with
        # no cancellation history, and the record names what is still applied.
        assert row(conn, request)["state"] == "removal_failed"
        assert conn.execute(
            "SELECT COUNT(*) FROM removal_events WHERE outcome = 'cancelled'"
        ).fetchone()[0] == 0
        assert recorded_effects(conn, request) == [
            {"kind": "unmonitor", "arr": "radarr", "resource_id": 1}
        ]
        assert case.monitored() is False
        # The exclusion could still be removed, and it was: the record only
        # keeps what is really still applied.
        assert [effect["kind"] for effect in recorded_effects(conn, request)] == [
            "unmonitor"
        ]
        assert ("DELETE", "/api/v3/exclusions/1") in [
            (request_["method"], request_["path"])
            for request_ in case.radarr.requests
        ]

        # Clearing the liability lets the same cancellation complete.
        case.radarr_data.always_fail.clear()
        cancelled = case.engine.undo(case.open_request())
        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() is True


def test_a_resource_already_gone_counts_as_reverted(conn, tmp_path):
    """A 404 means the effect no longer exists, so the cancel can proceed."""
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        case.radarr_data.movies = []

        cancelled = case.engine.undo(failed)

        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None


def test_a_revert_without_the_arr_configured_is_reported(conn, tmp_path):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        case.engine.reverter = ArrReverter(radarr=None, sonarr=None)

        with pytest.raises(SideEffectRevertError):
            case.engine.undo(failed)

        assert row(conn, request)["state"] == "removal_failed"
        assert recorded_effects(conn, request) == [
            {"kind": "unmonitor", "arr": "radarr", "resource_id": 1},
            {
                "kind": "exclusion", "arr": "radarr", "external_id": 2001,
                "exclusion_id": 1, "title": "Title 1",
            },
        ]


def test_an_unreadable_effects_record_refuses_the_cancel_without_touching_the_arr(
    conn, tmp_path
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        conn.execute(
            "UPDATE removal_requests SET applied_effects = 'broken' WHERE id = ?",
            (request.id,),
        )
        conn.commit()
        sent = len(case.radarr.requests)

        with pytest.raises(LifecycleError):
            case.engine.undo(request)

        assert row(conn, request)["state"] == "removal_failed"
        assert case.radarr.requests[sent:] == []


# -- the owner's action through the web --------------------------------------


@pytest.fixture()
def web_app(settings, owner_client_class):
    configured = replace(settings, scheduler_enabled=False)
    save_settings(configured)
    from almali_library_curator.app import create_app

    app = create_app(configured)
    app.config.update(
        TESTING=True, SECRET_KEY="web-test-session", WTF_CSRF_ENABLED=False
    )
    app.test_client_class = owner_client_class
    yield app


def _flashes(client):
    with client.session_transaction() as session:
        return [key for _, key in session.get("_flashes", [])]


def test_the_web_cancel_reports_a_revert_it_could_not_undo(web_app):
    """Without an arr to talk to the cancel is refused with the generic
    action failure: nothing is changed and nothing is claimed."""
    db = connect(web_app.config["SETTINGS"].database_path)
    try:
        library = seed_library(db, "1", "Movies")
        item = seed_item(db, library, "11", "Tenet")
        engine = LifecycleEngine(
            db, settings=web_app.config["SETTINGS"],
            clock=lambda: datetime.now(timezone.utc),
        )
        request = engine.mark_for_removal(
            make_candidate(item, "11", "Tenet"), marked_by="owner"
        )
        effects = [
            {"kind": "unmonitor", "arr": "radarr", "resource_id": 1},
            {
                "kind": "exclusion", "arr": "radarr", "external_id": 2001,
                "exclusion_id": 1, "title": "Tenet",
            },
        ]
        db.execute(
            "UPDATE removal_requests SET state = 'removal_failed', applied_effects = ?"
            " WHERE id = ?",
            (json.dumps(effects), request.id),
        )
        db.commit()

        client = web_app.test_client()
        response = client.post(f"/requests/{request.id}/undo")

        assert response.status_code == 303
        stored = db.execute(
            "SELECT state, applied_effects FROM removal_requests WHERE id = ?",
            (request.id,),
        ).fetchone()
        assert stored["state"] == "removal_failed"
        assert json.loads(stored["applied_effects"]) == effects
        assert db.execute(
            "SELECT COUNT(*) FROM removal_events WHERE outcome = 'cancelled'"
        ).fetchone()[0] == 0
        assert _flashes(client) == ["web.flash.action_failed"]
    finally:
        db.close()


# -- the production scan path ------------------------------------------------


def test_the_scan_pipeline_reverts_what_a_failed_removal_applied(conn, tmp_path):
    """The scan's own engine has the arr reverter wired (S3, #69).

    Without it, a cancellation that meets applied side effects fails the
    recheck instead of undoing them, so the title would stay unmonitored
    and excluded after the owner (or the final check) cancelled it.
    """
    from fake_plex_server import FakePlexDataset, FakePlexServer
    from almali_library_curator.scan.pipeline import default_steps

    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        assert case.monitored() is False
        user = seed_user(conn, "1", "Marc")
        seed_play(conn, user, "item-1", case.clock())
        sent = len(case.radarr_server.requests)

        data = FakePlexDataset()
        with FakePlexServer(data) as plex:
            settings = replace(
                case.settings,
                plex_base_url=plex.base_url,
                plex_token="test-token",
                radarr_base_url=case.radarr.base_url,
                radarr_api_key=case.radarr_data.api_key,
            )
            steps = default_steps(conn, settings, case.clock, None)

            report = steps["recheck"]()

        assert report["cancelled"] == 1
        assert row(conn, request)["state"] == "cancelled"
        assert row(conn, request)["applied_effects"] is None
        assert case.monitored() is True
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr_server.requests[sent:]
            if request_["method"] != "GET"
        ] == [
            ("PUT", "/api/v3/movie/1"),
            ("DELETE", "/api/v3/exclusions/1"),
        ]


# -- the claim before the revert (#99, #100) ---------------------------------


class HookedReverter:
    """The real reverter, with a hook that runs just before the first revert.

    The hook is where another actor gets to act while the cancellation is
    talking to the arr.
    """

    def __init__(self, inner, before_first_revert):
        self.inner = inner
        self.before_first_revert = before_first_revert
        self.reverted = 0

    def revert(self, effect):
        if self.reverted == 0:
            self.before_first_revert()
        self.reverted += 1
        self.inner.revert(effect)


class Crash(BaseException):
    """The process dies: nothing below the cancellation gets to clean up."""


def pass_the_retry(case, request):
    """The failed removal passes the final check again and awaits a retry,
    still carrying the effects its failure applied."""
    case.radarr_data.always_fail.clear()
    passed = [outcome.request.id for outcome in case.engine.run_recheck().passed]
    assert passed == [request.id]
    assert case.open_request().applied_effects != ()
    return passed


def test_a_deletion_cannot_claim_a_request_while_its_cancellation_reverts(
    conn, tmp_path
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        passed = pass_the_retry(case, request)
        retries = []
        case.engine.reverter = HookedReverter(
            case.engine.reverter,
            lambda: retries.append(case.executor.run_deletions(passed_ids=passed)),
        )

        cancelled = case.engine.undo(case.open_request())

        # The deletion that ran while the revert talked to the arr found
        # the request claimed by the cancellation and left it alone.
        assert retries[0].outcomes == ()
        assert cancelled.state is RemovalState.CANCELLED
        assert row(conn, request)["applied_effects"] is None
        assert [movie["id"] for movie in case.radarr_data.movies] == [1]
        assert case.monitored() is True
        assert case.exclusions() == []


def test_a_cancellation_that_lost_its_claim_reverts_nothing(
    conn, tmp_path, monkeypatch
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        passed = pass_the_retry(case, request)
        reload = case.engine._assert_cancelable
        sent = []

        def reload_then_lose_the_race(stale):
            current = reload(stale)
            # The retry claims and finishes the removal right after the
            # cancellation read the request.
            outcome = case.executor.run_deletions(passed_ids=passed).outcomes[0]
            assert outcome.status == "deleted"
            sent.append(len(case.radarr.requests))
            return current

        monkeypatch.setattr(case.engine, "_assert_cancelable", reload_then_lose_the_race)

        with pytest.raises(RequestStateError):
            case.engine.undo(case.open_request())

        # Not a single arr request: the exclusion of the deleted movie stays.
        assert case.radarr.requests[sent[0]:] == []
        assert row(conn, request)["state"] == "deleted"
        assert case.exclusions() != []
        assert conn.execute(
            "SELECT COUNT(*) FROM removal_events WHERE outcome = 'cancelled'"
        ).fetchone()[0] == 0


def test_the_revert_undoes_exactly_what_the_live_record_names(
    conn, tmp_path, monkeypatch
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        reload = case.engine._assert_cancelable

        def reload_then_another_writer_trims_the_record(stale):
            current = reload(stale)
            conn.execute(
                "UPDATE removal_requests SET applied_effects = ? WHERE id = ?",
                (json.dumps([{"kind": "unmonitor", "arr": "radarr", "resource_id": 1}]),
                 request.id),
            )
            conn.commit()
            return current

        monkeypatch.setattr(
            case.engine, "_assert_cancelable", reload_then_another_writer_trims_the_record
        )
        sent = len(case.radarr.requests)

        cancelled = case.engine.undo(case.open_request())

        assert cancelled.state is RemovalState.CANCELLED
        assert [
            (request_["method"], request_["path"])
            for request_ in case.radarr.requests[sent:]
            if request_["method"] != "GET"
        ] == [("PUT", "/api/v3/movie/1")]
        # The record no longer named the exclusion, so it was not touched.
        assert case.exclusions() != []


def test_a_cancellation_interrupted_mid_revert_is_recovered_by_the_next_recheck(
    conn, tmp_path
):
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        failed = case.fail_the_removal()
        real = case.engine.reverter

        def crash():
            raise Crash()

        case.engine.reverter = HookedReverter(real, crash)
        with pytest.raises(Crash):
            case.engine.undo(failed)

        # The claim is durable, so nothing else can take the request...
        claimed = case.open_request()
        assert claimed.state is RemovalState.CANCELLING
        with pytest.raises(RequestStateError):
            case.engine.undo(claimed)
        # ...and a fresh claim may still be reverting: it is left alone.
        case.engine.reverter = real
        case.engine.run_recheck()
        assert case.open_request().state is RemovalState.CANCELLING

        # A stale claim returns to the failure state, which must pass the
        # final check again, with every recorded effect still named.
        case.clock.advance(minutes=31)
        case.engine.run_recheck()
        recovered = case.open_request()
        assert recovered.state is RemovalState.RECHECK_PASSED
        assert [effect.kind for effect in recovered.applied_effects] == [
            "unmonitor", "exclusion",
        ]
        assert case.monitored() is False

        cancelled = case.engine.undo(recovered)
        assert cancelled.state is RemovalState.CANCELLED
        assert case.monitored() is True
        assert case.exclusions() == []


def _claim_written_at(conn, request, moment):
    conn.execute(
        "UPDATE removal_requests SET state = 'cancelling', updated_at = ? WHERE id = ?",
        (moment.isoformat(), request.id),
    )
    conn.commit()


def test_a_fresh_claim_written_in_utc_survives_a_recheck_clock_with_an_offset(
    conn, tmp_path
):
    # The web writes UTC timestamps, the scan its local time: the age of a
    # claim is a time difference, never a string comparison.
    from datetime import timedelta

    plus_one = timezone(timedelta(hours=1))
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        utc_now = case.clock.now.astimezone(timezone.utc)
        _claim_written_at(conn, request, utc_now - timedelta(minutes=11))
        case.clock.now = utc_now.astimezone(plus_one)

        assert case.engine.recover_interrupted_cancellations() == 0
        assert row(conn, request)["state"] == "cancelling"


def test_a_stale_claim_written_with_an_offset_is_recovered_by_a_utc_clock(
    conn, tmp_path
):
    from datetime import timedelta

    plus_one = timezone(timedelta(hours=1))
    with make_removal(conn, tmp_path).attached() as case:
        request = case.mark()
        case.fail_the_removal()
        utc_now = case.clock.now.astimezone(timezone.utc)
        _claim_written_at(conn, request, (utc_now - timedelta(minutes=40)).astimezone(plus_one))
        case.clock.now = utc_now

        assert case.engine.recover_interrupted_cancellations() == 1
        assert row(conn, request)["state"] == "removal_failed"
