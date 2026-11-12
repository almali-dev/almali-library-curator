"""S15: optional Sonarr and Radarr, with Plex as the deletion fallback.

Every rule runs through the public lifecycle API (mark, final check,
``DeletionExecutor.run_deletions``) against a real migrated database, real
files on disk and the fake Plex, Sonarr and Radarr HTTP servers.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

import pytest

from almali_library_curator.detection.models import Unit
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.plex import PlexClient
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_plex_server import FakePlexDataset, FakePlexServer
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from lifecycle_helpers import (
    FakeCollections, FrozenClock, default_settings, make_candidate,
    seed_episode, seed_item, seed_library,
)

WRITES = ("PUT", "POST", "DELETE")


class Scenario:
    """One library, real files, and the three fake servers.

    ``radarr``/``sonarr`` say whether that service is configured (the
    executor gets a client); the ``*_deletes`` switches come from Settings.
    """

    def __init__(self, conn, tmp_path, *, unit=Unit.MOVIE, radarr=False, sonarr=False,
                 dry_run=False, **settings):
        self.conn = conn
        self.root = tmp_path
        self.unit = unit
        self.clock = FrozenClock()
        self.settings = default_settings(
            dry_run=dry_run,
            plex_path_map=(("/data", str(tmp_path)),),
            media_root=str(tmp_path),
            **settings)
        self.engine = LifecycleEngine(conn, settings=self.settings, clock=self.clock,
                                      collections=FakeCollections())
        self.library = seed_library(conn, "1", "Library")
        self.configured = {"radarr": radarr, "sonarr": sonarr}
        self.plex = FakePlexDataset()
        self.radarr = FakeRadarrDataset()
        self.sonarr = FakeSonarrDataset()
        self.files = {}  # Plex rating key -> the file Plex would delete.
        self.plex.on_delete_media = lambda key: self.files[key].unlink(missing_ok=True)
        self.radarr.on_delete_movie = lambda _id: self.files["item-1"].unlink(missing_ok=True)
        self.sonarr.on_delete_series = lambda _id: [
            path.unlink(missing_ok=True) for path in self.files.values()]
        self.passed_ids = []

    def add(self, *, linked=True, seasons=(1,), episodes=2):
        key, title = "item-1", "Title 1"
        item = seed_item(self.conn, self.library, key, title,
                         item_type="movie" if self.unit is Unit.MOVIE else "series")
        if self.unit is Unit.MOVIE:
            path = self.root / "movie.mkv"
            path.write_bytes(b"x" * 17)
            self.files[key] = path
            self.conn.execute(
                "UPDATE media_items SET file_path = ?, radarr_id = ?, tmdb_id = ? WHERE id = ?",
                (str(path), 1 if linked else None, 2001 if linked else None, item))
            self.radarr.movies.append(movie_resource(1, title, path="/data"))
        else:
            self.conn.execute(
                "UPDATE media_items SET sonarr_id = ?, tvdb_id = ? WHERE id = ?",
                (1 if linked else None, 1001 if linked else None, item))
            self.sonarr.series.append(series_resource(1, title, path="/data"))
            for season in (1, 2):
                seed_item(self.conn, self.library, f"{key}-s{season}", f"Season {season}",
                          item_type="season", parent_rating_key=key, season_number=season)
                for number in range(1, episodes + 1):
                    episode = seed_episode(self.conn, key, season, number)
                    path = self.root / f"s{season}e{number}.mkv"
                    path.write_bytes(b"x" * 11)
                    self.files[episode] = path
                    self.conn.execute("UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                                      (str(path), episode))
        self.conn.commit()
        candidate = make_candidate(item, key, title, unit=self.unit,
                                   seasons=seasons if self.unit is Unit.SEASONS else ())
        request = self.engine.mark_for_removal(candidate, marked_by="owner")
        self.clock.advance(days=14)
        self.passed_ids = [o.request.id for o in self.engine.run_recheck().passed]
        return request

    @contextmanager
    def servers(self):
        with ExitStack() as stack:
            plex = stack.enter_context(FakePlexServer(self.plex))
            radarr = stack.enter_context(FakeRadarrServer(self.radarr))
            sonarr = stack.enter_context(FakeSonarrServer(self.sonarr))
            from almali_library_curator.lifecycle.deletion import DeletionExecutor

            executor = DeletionExecutor(
                self.engine,
                plex=PlexClient(plex.base_url, "test-token", rate_limit_per_second=0,
                                max_retries=0, backoff_seconds=0),
                radarr=RadarrClient(radarr.base_url, self.radarr.api_key,
                                    rate_limit_per_second=0, max_retries=0)
                if self.configured["radarr"] else None,
                sonarr=SonarrClient(sonarr.base_url, self.sonarr.api_key,
                                    rate_limit_per_second=0, max_retries=0)
                if self.configured["sonarr"] else None,
            )
            yield executor, plex, radarr, sonarr

    def run(self):
        with self.servers() as (executor, plex, radarr, sonarr):
            report = executor.run_deletions(passed_ids=self.passed_ids)
            self.plex_deletes = [r["path"] for r in plex.requests if r["method"] == "DELETE"]
            self.arr_writes = [(r["method"], r["path"])
                               for server in (radarr, sonarr) for r in server.requests
                               if r["method"] in WRITES]
            self.arr_requests = len(radarr.requests) + len(sonarr.requests)
        return report

    def row(self, request):
        return self.conn.execute("SELECT * FROM removal_requests WHERE id = ?",
                                 (request.id,)).fetchone()


# -- rule 1 and rule 2: routing per unit type and per switch ----------------

@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_without_the_arr_plex_removes_the_item_and_no_arr_is_called(conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit)
    scenario.add()
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert scenario.plex_deletes
    assert scenario.arr_requests == 0
    assert not any(path.exists() for path in scenario.files.values())


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_a_configured_and_enabled_arr_wins_and_plex_deletes_nothing(conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit, radarr=True, sonarr=True)
    scenario.add()
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert scenario.plex_deletes == []
    expected = ("DELETE", "/api/v3/movie/1") if unit is Unit.MOVIE else (
        "DELETE", "/api/v3/series/1")
    assert expected in scenario.arr_writes


@pytest.mark.parametrize("unit,switch", [(Unit.MOVIE, "radarr_deletes"),
                                         (Unit.SERIES, "sonarr_deletes")])
def test_a_switched_off_arr_is_never_used_for_deletion(conn, tmp_path, unit, switch):
    scenario = Scenario(conn, tmp_path, unit=unit, radarr=True, sonarr=True, **{switch: False})
    scenario.add()
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert scenario.plex_deletes
    assert scenario.arr_writes == []


def test_switching_radarr_off_leaves_sonarr_in_charge_of_series(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES, radarr=True, sonarr=True,
                        radarr_deletes=False)
    scenario.add()
    scenario.run()
    assert scenario.plex_deletes == []
    assert ("DELETE", "/api/v3/series/1") in scenario.arr_writes


@pytest.mark.parametrize("unit,service", [(Unit.MOVIE, "Radarr"), (Unit.SERIES, "Sonarr")])
def test_a_configured_arr_without_the_identity_fails_and_never_falls_back_to_plex(
        conn, tmp_path, unit, service):
    scenario = Scenario(conn, tmp_path, unit=unit, radarr=True, sonarr=True)
    request = scenario.add(linked=False)
    report = scenario.run()
    key = f"removal.failed.{service.lower()}_identity"
    assert [(o.status, o.error) for o in report.outcomes] == [("failed", key)]
    assert scenario.plex_deletes == []
    assert scenario.arr_writes == []
    assert all(path.exists() for path in scenario.files.values())
    row = scenario.row(request)
    assert (row["state"], row["last_error"]) == ("removal_failed", key)
    for locale in ("en", "ca", "es"):
        sentence = translate(key, locale)
        assert sentence != key and service in sentence
    # The failure retries: the next final check passes it through again.
    scenario.clock.advance(days=1)
    assert [o.request.id for o in scenario.engine.run_recheck().passed] == [request.id]


# -- rule 4: the Plex requests ----------------------------------------------

def test_plex_removes_a_movie_with_one_request_for_the_movie(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    scenario.run()
    assert scenario.plex_deletes == ["/library/metadata/item-1"]


def test_plex_removes_a_season_episode_by_episode_and_leaves_the_rest(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add(seasons=(1,))
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert sorted(scenario.plex_deletes) == [
        "/library/metadata/item-1-s1e1", "/library/metadata/item-1-s1e2"]
    assert scenario.files["item-1-s2e1"].exists() and scenario.files["item-1-s2e2"].exists()
    assert not scenario.files["item-1-s1e1"].exists()


def test_plex_removes_a_series_episode_by_episode_and_never_the_container(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    scenario.add()
    scenario.run()
    assert sorted(scenario.plex_deletes) == sorted(
        f"/library/metadata/item-1-s{s}e{e}" for s in (1, 2) for e in (1, 2))
    assert "/library/metadata/item-1" not in scenario.plex_deletes
    assert not any(path.startswith("/library/metadata/item-1-s1") and path.endswith("-s1")
                   for path in scenario.plex_deletes)


def test_an_item_plex_no_longer_knows_still_needs_its_files_gone(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.plex.missing_media.add("item-1")
    report = scenario.run()
    # Plex's 404 is not proof: the file is still on disk, so it fails.
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.files_present")]
    assert scenario.row(request)["state"] == "removal_failed"


# -- rule 6: the verified outcome --------------------------------------------

def test_the_plex_path_reports_deleted_only_when_every_file_is_gone(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    request = scenario.add(seasons=(1,))
    # Plex answers 200 but one file survives on disk.
    scenario.plex.on_delete_media = lambda key: (
        None if key == "item-1-s1e2" else scenario.files[key].unlink())
    report = scenario.run()
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.files_present")]
    row = scenario.row(request)
    assert (row["state"], row["deleted_at"]) == ("removal_failed", None)
    event = conn.execute("SELECT outcome, freed_bytes FROM removal_events").fetchone()
    assert (event["outcome"], event["freed_bytes"]) == ("failed", 0)


def test_the_plex_path_counts_freed_space_by_inode(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    outside = tmp_path / "outside-link.mkv"
    outside.hardlink_to(scenario.files["item-1"])
    report = scenario.run()
    outcome = report.outcomes[0]
    assert (outcome.status, outcome.size_bytes, outcome.freed_bytes) == ("deleted", 17, 0)
    event = conn.execute("SELECT outcome, size_bytes, freed_bytes FROM removal_events").fetchone()
    assert tuple(event) == ("deleted", 17, 0)


# -- rule 7: a refused deletion ----------------------------------------------

def test_a_refused_plex_deletion_names_the_setting_and_deletes_nothing(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.plex.fail_always("/library/metadata/item-1", 403)
    report = scenario.run()
    key = "removal.failed.plex_refused"
    assert [(o.status, o.error) for o in report.outcomes] == [("failed", key)]
    assert scenario.files["item-1"].exists()
    row = scenario.row(request)
    assert (row["state"], row["last_error"]) == ("removal_failed", key)
    assert "Allow media deletion" in translate(key, "en")
    for locale in ("ca", "es"):
        assert translate(key, locale) != key
    # Still listed in Leaving soon (removal_failed is an open state) and retried.
    scenario.clock.advance(days=1)
    assert [o.request.id for o in scenario.engine.run_recheck().passed] == [request.id]


def test_a_rejected_token_is_not_reported_as_a_refused_deletion(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    scenario.plex.required_token = "another-token"
    report = scenario.run()
    assert [o.error for o in report.outcomes] == ["removal.failed.plex_auth"]
    assert scenario.files["item-1"].exists()


def test_an_unreachable_plex_is_not_reported_as_a_refused_deletion(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    scenario.plex.fail_always("/library/metadata/item-1", 503)
    report = scenario.run()
    assert [o.error for o in report.outcomes] == ["removal.failed.plex_unavailable"]
    assert scenario.files["item-1"].exists()


# -- rule 9: dry run and curated scope ---------------------------------------

@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SERIES])
def test_a_dry_run_sends_no_delete_request_at_all(conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit, dry_run=True)
    scenario.add()
    scenario.passed_ids = [r["id"] for r in conn.execute("SELECT id FROM removal_requests")]
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["would_delete"]
    assert scenario.plex_deletes == []
    assert all(path.exists() for path in scenario.files.values())


def test_an_item_outside_the_curated_scope_is_never_deleted_through_plex(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    # The owner deselects the library before the next final check.
    scenario.engine = LifecycleEngine(
        conn, settings=default_settings(curated_libraries=("99",)), clock=scenario.clock,
        collections=FakeCollections())
    scenario.conn.execute("UPDATE removal_requests SET state = 'marked' WHERE id = ?",
                          (request.id,))
    scenario.conn.commit()
    scenario.passed_ids = [o.request.id for o in scenario.engine.run_recheck().passed]
    report = scenario.run()
    assert report.outcomes == ()
    assert scenario.plex_deletes == []
    assert scenario.files["item-1"].exists()
