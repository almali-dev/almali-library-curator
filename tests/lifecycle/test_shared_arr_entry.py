"""#136: never remove an arr entry or a file another curated row still uses.

Two `media_items` rows can carry the same ``radarr_id``/``sonarr_id`` (the
linking pass has no uniqueness check), or point at the same ``file_path``
(two libraries over one file). Removing one of them must not take the
other copy's files, monitoring or import exclusion with it: the deletion
preflight refuses it, in the preview and in the real run, before anything
is sent to Radarr, Sonarr or Plex.

Every case runs through the public lifecycle API (mark, final check,
``DeletionExecutor.run_deletions``) against a real migrated database, real
files on disk and the fake Plex, Sonarr and Radarr HTTP servers.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

import pytest

from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.plex import PlexClient
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_plex_server import (
    FakePlexDataset, FakePlexServer, episode as plex_episode, movie as plex_movie,
    season as plex_season, series as plex_series,
)
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer, series_resource
from lifecycle_helpers import (
    FakeCollections, FrozenClock, default_settings, make_candidate,
    seed_episode, seed_item, seed_library,
)

WRITES = ("PUT", "POST", "DELETE")


class Scenario:
    """Two libraries over the same fake services, with real files on disk.

    ``radarr``/``sonarr`` say whether that service is configured (the
    executor gets a client), so the Plex route is exercised by leaving both
    off.
    """

    def __init__(self, conn, tmp_path, *, unit=Unit.MOVIE, radarr=False, sonarr=False,
                 dry_run=False):
        self.conn = conn
        self.root = tmp_path
        self.unit = unit
        self.clock = FrozenClock()
        self.settings = default_settings(
            dry_run=dry_run,
            plex_path_map=(("/data", str(tmp_path)),),
            media_root=str(tmp_path))
        self.engine = LifecycleEngine(conn, settings=self.settings, clock=self.clock,
                                      collections=FakeCollections())
        self.library = seed_library(conn, "1", "Library")
        self.other_library = seed_library(conn, "2", "Library 4K")
        self.configured = {"radarr": radarr, "sonarr": sonarr}
        self.plex = FakePlexDataset()
        self.radarr = FakeRadarrDataset()
        self.sonarr = FakeSonarrDataset()
        self.files: dict[str, object] = {}  # Plex rating key -> the file the arr/Plex removes.
        self.episode_files: dict[int, object] = {}
        self.next_episode_file = 100
        self.plex.on_delete_media = lambda key: self.files[key].unlink(missing_ok=True)
        self.radarr.on_delete_movie = lambda identifier: self.files["item-1"].unlink(
            missing_ok=True)
        # The real Sonarr removes the whole series folder, which holds the
        # files of both rows when two libraries index the same series.
        self.sonarr.on_delete_series = self.delete_series_folder
        self.sonarr.on_delete_episode_files = self.delete_episode_files

    def delete_series_folder(self, _identifier):
        for path in self.files.values():
            path.unlink(missing_ok=True)

    def delete_episode_files(self, identifiers):
        for identifier in identifiers:
            self.episode_files[identifier].unlink(missing_ok=True)

    def add_movie(self, key, library, title, *, radarr_id=None, tmdb_id=None, filename=None):
        item = seed_item(self.conn, library, key, title, item_type="movie")
        path = self.root / (filename or f"{key}.mkv")
        path.write_bytes(b"x" * 17)
        self.conn.execute(
            "UPDATE media_items SET file_path = ?, radarr_id = ?, tmdb_id = ? WHERE id = ?",
            (str(path), radarr_id, tmdb_id, item))
        self.plex.metadata[key] = plex_movie(key, title)
        self.files[key] = path
        self.conn.commit()
        return item

    def add_series(self, key, library, title, *, sonarr_id=None, tvdb_id=None, seasons=(1,)):
        item = seed_item(self.conn, library, key, title, item_type="series")
        self.conn.execute(
            "UPDATE media_items SET sonarr_id = ?, tvdb_id = ? WHERE id = ?",
            (sonarr_id, tvdb_id, item))
        self.plex.metadata[key] = plex_series(key, title)
        for season in seasons:
            season_key = f"{key}-s{season}"
            seed_item(self.conn, library, season_key, f"Season {season}",
                      item_type="season", parent_rating_key=key, season_number=season)
            self.plex.metadata[season_key] = plex_season(season_key, key, season)
            episode_key = seed_episode(self.conn, key, season, 1)
            self.plex.metadata[episode_key] = plex_episode(
                episode_key, key, season_key, 1)
            # Both rows share one series folder, exactly like a real Sonarr
            # series indexed by two Plex libraries.
            path = self.root / f"{key}-s{season}e1.mkv"
            path.write_bytes(b"x" * 17)
            self.conn.execute(
                "UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                (str(path), episode_key))
            self.next_episode_file += 1
            self.sonarr.episode_files.append({
                "id": self.next_episode_file,
                "seriesId": sonarr_id,
                "seasonNumber": season,
                "path": f"/data/{path.name}",
            })
            self.episode_files[self.next_episode_file] = path
            self.files[episode_key] = path
        self.conn.commit()
        return item

    def mark(self, item, key, title, *, unit=None, seasons=()):
        unit = unit or self.unit
        candidate = make_candidate(item, key, title, unit=unit, seasons=seasons)
        request = self.engine.mark_for_removal(candidate, marked_by="owner")
        self.clock.advance(days=14)
        return request

    def passed_ids(self):
        return [outcome.request.id for outcome in self.engine.run_recheck().passed]

    @contextmanager
    def servers(self):
        with ExitStack() as stack:
            plex = stack.enter_context(FakePlexServer(self.plex))
            radarr = stack.enter_context(FakeRadarrServer(self.radarr))
            sonarr = stack.enter_context(FakeSonarrServer(self.sonarr))
            from almali_library_curator.lifecycle.deletion import DeletionExecutor

            yield DeletionExecutor(
                self.engine,
                plex=PlexClient(plex.base_url, "test-token", rate_limit_per_second=0,
                                max_retries=0, backoff_seconds=0),
                radarr=RadarrClient(radarr.base_url, self.radarr.api_key,
                                    rate_limit_per_second=0, max_retries=0)
                if self.configured["radarr"] else None,
                sonarr=SonarrClient(sonarr.base_url, self.sonarr.api_key,
                                    rate_limit_per_second=0, max_retries=0)
                if self.configured["sonarr"] else None,
            ), plex, radarr, sonarr

    def run(self, ids):
        with self.servers() as (executor, plex, radarr, sonarr):
            report = executor.run_deletions(passed_ids=ids)
            self.plex_deletes = [r["path"] for r in plex.requests if r["method"] == "DELETE"]
            self.plex_requests = len(plex.requests)
            self.arr_writes = [(r["method"], r["path"])
                               for server in (radarr, sonarr) for r in server.requests
                               if r["method"] in WRITES]
            self.arr_requests = len(radarr.requests) + len(sonarr.requests)
        return report

    def row(self, request):
        return self.conn.execute(
            "SELECT * FROM removal_requests WHERE id = ?", (request.id,)).fetchone()


def two_series_over_one_sonarr_entry(scenario, *, second_seasons=(1,)):
    """Rows A and B in two libraries, both on Sonarr series 1, one folder."""
    first = scenario.add_series("item-1", scenario.library, "Title 1",
                                sonarr_id=1, tvdb_id=1001)
    second = scenario.add_series("item-2", scenario.other_library, "Title 1 4K",
                                 sonarr_id=1, tvdb_id=1001, seasons=second_seasons)
    scenario.sonarr.series.append(series_resource(1, "Title 1", path="/data"))
    scenario.second_item = second
    return first


def two_movies_over_one_radarr_entry(scenario):
    """Rows A and B in two libraries, both on Radarr movie 10, own folders."""
    first = scenario.add_movie("item-1", scenario.library, "Title 1",
                               radarr_id=10, tmdb_id=2010)
    second = scenario.add_movie("item-2", scenario.other_library, "Title 1 4K",
                                radarr_id=10, tmdb_id=2010)
    scenario.radarr.movies.append(movie_resource(10, "Title 1", path="/data"))
    scenario.second_item = second
    return first


def test_a_series_removal_another_library_shares_is_refused_without_a_sonarr_call(
        conn, tmp_path):
    # S1 (#136): Sonarr series 1 is two curated Plex rows. Deleting it with
    # files takes the other library's files with it and ends "deleted".
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES, sonarr=True)
    item = two_series_over_one_sonarr_entry(scenario)
    request = scenario.mark(item, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error == "removal.failed.shared_arr_entry"
    assert scenario.arr_writes == []
    assert scenario.arr_requests == 0
    assert scenario.sonarr.import_exclusions == []
    assert scenario.sonarr.series[0]["monitored"] is True
    assert scenario.plex_requests == 0
    assert all(path.exists() for path in scenario.files.values())
    row = scenario.row(request)
    assert (row["state"], row["last_error"]) == (
        "removal_failed", "removal.failed.shared_arr_entry")
    event = conn.execute("SELECT * FROM removal_events").fetchone()
    assert (event["outcome"], event["error"]) == (
        "failed", "removal.failed.shared_arr_entry")


def test_a_season_removal_another_library_shares_is_refused_without_a_sonarr_call(
        conn, tmp_path):
    # S1 (#136): the Sonarr route of a season scope shares the same series
    # entry, so it is refused too, before any episode file is deleted.
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS, sonarr=True)
    # The other library's copy lives in another season, so today the marked
    # season-1 scope passes the ownership check and its removal goes out.
    item = two_series_over_one_sonarr_entry(scenario, second_seasons=(2,))
    request = scenario.mark(item, "item-1", "Title 1", unit=Unit.SEASONS, seasons=(1,))
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.shared_arr_entry")]
    assert scenario.arr_writes == [] and scenario.arr_requests == 0
    assert all(path.exists() for path in scenario.files.values())
    assert scenario.row(request)["state"] == "removal_failed"


def test_a_movie_removal_another_library_shares_is_refused_without_a_radarr_call(
        conn, tmp_path):
    # S1 (#136): Radarr movie 10 is two curated Plex rows. Its removal must
    # not unmonitor, exclude or delete while the other copy uses it.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE, radarr=True)
    item = two_movies_over_one_radarr_entry(scenario)
    request = scenario.mark(item, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error == "removal.failed.shared_arr_entry"
    assert scenario.arr_writes == [] and scenario.arr_requests == 0
    assert scenario.radarr.import_exclusions == []
    assert scenario.radarr.movies[0]["monitored"] is True
    assert scenario.plex_requests == 0
    assert all(path.exists() for path in scenario.files.values())
    assert scenario.row(request)["state"] == "removal_failed"


def test_a_file_another_library_points_at_is_refused_on_the_plex_route(conn, tmp_path):
    # S2 (#136): two libraries over one file. Without an arr the removal
    # goes through Plex, and the shared file must survive.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE)
    first = scenario.add_movie("item-1", scenario.library, "Title 1",
                               filename="shared.mkv")
    second = scenario.add_movie("item-2", scenario.other_library, "Title 1 4K",
                                filename="shared.mkv")
    assert second != first
    request = scenario.mark(first, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error == "removal.failed.shared_file"
    assert scenario.plex_requests == 0
    assert scenario.arr_requests == 0
    assert (tmp_path / "shared.mkv").is_file()
    row = scenario.row(request)
    assert (row["state"], row["last_error"]) == ("removal_failed", "removal.failed.shared_file")


def test_a_shared_episode_file_is_refused_on_the_plex_route(conn, tmp_path):
    # S2 (#136): two series rows over one episode file, on the Plex route.
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    first = scenario.add_series("item-1", scenario.library, "Title 1")
    second = scenario.add_series("item-2", scenario.other_library, "Title 1 4K")
    # Both libraries index the very same episode file.
    shared = scenario.files["item-1-s1e1"]
    scenario.files["item-2-s1e1"] = shared
    scenario.episode_files[102] = shared
    conn.execute("UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                 (str(shared), "item-2-s1e1"))
    conn.commit()
    assert second != first
    request = scenario.mark(first, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.shared_file")]
    assert scenario.plex_requests == 0
    assert shared.is_file()
    assert scenario.row(request)["state"] == "removal_failed"


def test_a_movie_removal_ignores_a_sharing_row_plex_no_longer_has(conn, tmp_path):
    # R3 correction (#136): a proven-absent duplicate is not a live user of
    # the Radarr entry, so it must not refuse the removal forever.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE, radarr=True)
    item = two_movies_over_one_radarr_entry(scenario)
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?",
                 (scenario.second_item,))
    conn.commit()
    request = scenario.mark(item, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert ("DELETE", "/api/v3/movie/10") in scenario.arr_writes
    assert not scenario.files["item-1"].exists()
    assert scenario.row(request)["state"] == "deleted"


def test_a_series_removal_ignores_a_sharing_row_plex_no_longer_has(conn, tmp_path):
    # R3 correction (#136): the same for the Sonarr entry of a series.
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES, sonarr=True)
    item = two_series_over_one_sonarr_entry(scenario)
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?",
                 (scenario.second_item,))
    conn.commit()
    request = scenario.mark(item, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert ("DELETE", "/api/v3/series/1") in scenario.arr_writes
    assert scenario.row(request)["state"] == "deleted"


def test_a_shared_file_plex_no_longer_has_does_not_refuse_the_plex_route(conn, tmp_path):
    # R3 correction (#136): a stale row over the same file is not a live
    # dependency, so the Plex route goes through as it would without it.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE)
    first = scenario.add_movie("item-1", scenario.library, "Title 1",
                               filename="shared.mkv")
    second = scenario.add_movie("item-2", scenario.other_library, "Title 1 4K",
                                filename="shared.mkv")
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?", (second,))
    conn.commit()
    request = scenario.mark(first, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert scenario.plex_deletes == ["/library/metadata/item-1"]
    assert not (tmp_path / "shared.mkv").exists()
    assert scenario.row(request)["state"] == "deleted"


def test_the_preview_refuses_a_shared_arr_entry_with_the_same_reason(conn, tmp_path):
    # S1 (#136): the preview reports the refusal the real run records, and
    # sends no mutating request at all.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE, radarr=True, dry_run=True)
    item = two_movies_over_one_radarr_entry(scenario)
    scenario.mark(item, "item-1", "Title 1")
    before = list(conn.iterdump())
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.shared_arr_entry")]
    assert scenario.arr_writes == []
    assert scenario.arr_requests == 0
    assert list(conn.iterdump()) == before
    assert all(path.exists() for path in scenario.files.values())


def test_a_shared_arr_entry_is_not_a_refusal_on_the_plex_route(conn, tmp_path):
    # Triangulation (#136): S1 is about the arr route. Without Radarr the
    # removal is Plex's, which deletes only this item's rating key and
    # files, so a stale shared radarr_id must not block an unrelated copy.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE)
    first = scenario.add_movie("item-1", scenario.library, "Title 1",
                               radarr_id=10, tmdb_id=2010)
    second = scenario.add_movie("item-2", scenario.other_library, "Title 1 4K",
                                radarr_id=10, tmdb_id=2010)
    assert second != first
    scenario.mark(first, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert scenario.plex_deletes == ["/library/metadata/item-1"]
    assert not scenario.files["item-1"].exists()
    assert scenario.files["item-2"].is_file()


def test_a_removal_no_other_row_shares_deletes_exactly_as_before(conn, tmp_path):
    # The control (#136): the guard is about sharing, so a curated row that
    # shares nothing with this removal behaves exactly like today.
    scenario = Scenario(conn, tmp_path, unit=Unit.MOVIE, radarr=True)
    item = scenario.add_movie("item-1", scenario.library, "Title 1",
                              radarr_id=10, tmdb_id=2010)
    # Another library holds a different movie on a different arr entry.
    scenario.add_movie("item-2", scenario.other_library, "Other", radarr_id=11,
                       tmdb_id=2011)
    scenario.radarr.movies.append(movie_resource(10, "Title 1", path="/data"))
    request = scenario.mark(item, "item-1", "Title 1")
    report = scenario.run(scenario.passed_ids())
    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert ("DELETE", "/api/v3/movie/10") in scenario.arr_writes
    assert not scenario.files["item-1"].exists()
    assert scenario.files["item-2"].is_file()
    assert scenario.row(request)["state"] == "deleted"
