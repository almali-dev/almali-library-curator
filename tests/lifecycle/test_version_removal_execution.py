"""#133 T6: the executor removes one version of a movie, and only that file.

S1: the same safety as every removal: dry run, the kill switch, and
"deleted" claimed only after the file is confirmed gone from disk; the
other versions stay. S3 (owner decision D2 = a): when the version is the
file Radarr manages, the removal is refused and Radarr is never changed.
A version is always removed through Plex, never through Radarr, whose
movie delete would take every version.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

import pytest

from almali_library_curator.lifecycle import LifecycleEngine
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from almali_library_curator.lifecycle.safety import set_kill_switch
from almali_library_curator.plex import PlexClient
from almali_library_curator.radarr import RadarrClient
from fake_plex_server import FakePlexDataset, FakePlexServer, movie as plex_movie
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from lifecycle_helpers import FakeCollections, FrozenClock, default_settings, seed_item, seed_library

WRITES = ("PUT", "POST", "DELETE")


class World:
    """One movie with a 4K and a 1080p version, real files, fake servers."""

    def __init__(self, conn, tmp_path, *, radarr=False, dry_run=False, managed="4k",
                 **settings):
        self.conn = conn
        self.root = tmp_path
        self.clock = FrozenClock()
        self.settings = default_settings(
            dry_run=dry_run, plex_path_map=(("/data", str(tmp_path)),),
            media_root=str(tmp_path), **settings)
        self.engine = LifecycleEngine(conn, settings=self.settings, clock=self.clock,
                                      collections=FakeCollections())
        library = seed_library(conn, "1", "Movies")
        self.item = seed_item(conn, library, "11", "Heat")
        self.four_k = tmp_path / "Heat 2160p.mkv"
        self.hd = tmp_path / "Heat 1080p.mkv"
        self.four_k.write_bytes(b"k" * 40)
        self.hd.write_bytes(b"h" * 12)
        for position, (media_id, path, resolution, codec) in enumerate(
                [(501, self.four_k, "4k", "hevc"), (502, self.hd, "1080", "h264")]):
            conn.execute(
                "INSERT INTO media_versions (media_item_id, position, plex_media_id,"
                " resolution, video_codec, size_bytes, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.item, position, media_id, resolution, codec, path.stat().st_size,
                 str(path)))
        conn.execute("UPDATE media_items SET file_path = ?, radarr_id = ?, tmdb_id = 2001"
                     " WHERE id = ?", (str(self.four_k), 1 if radarr else None, self.item))
        conn.commit()
        self.plex = FakePlexDataset()
        self.plex.metadata["11"] = plex_movie("11", "Heat", Media=[
            {"id": 501, "videoResolution": "4k",
             "Part": [{"id": 1, "file": "/data/Heat 2160p.mkv", "size": 40}]},
            {"id": 502, "videoResolution": "1080",
             "Part": [{"id": 2, "file": "/data/Heat 1080p.mkv", "size": 12}]},
        ])
        self.plex.on_delete_version = lambda key, media_id: (
            self.hd if media_id == 502 else self.four_k).unlink(missing_ok=True)
        self.radarr_on = radarr
        self.radarr = FakeRadarrDataset()
        managed_path = "/data/Heat 2160p.mkv" if managed == "4k" else "/data/Heat 1080p.mkv"
        self.radarr.movies.append(movie_resource(1, "Heat", tmdbId=2001, path="/data",
                                                 movieFile={"id": 9, "path": managed_path}))

    def mark_hd(self):
        request = self.engine.mark_version_for_removal(
            self.item, 502, str(self.hd), marked_by="owner")
        self.clock.advance(days=15)
        self.passed = [o.request.id for o in self.engine.run_recheck().passed]
        assert self.passed == [request.id]
        return request

    @contextmanager
    def executor(self):
        with ExitStack() as stack:
            plex = stack.enter_context(FakePlexServer(self.plex))
            radarr = stack.enter_context(FakeRadarrServer(self.radarr))
            yield DeletionExecutor(
                self.engine,
                plex=PlexClient(plex.base_url, "test-token", rate_limit_per_second=0,
                                max_retries=0, backoff_seconds=0,
                                path_map=(("/data", str(self.root)),)),
                radarr=RadarrClient(radarr.base_url, self.radarr.api_key,
                                    rate_limit_per_second=0, max_retries=0)
                if self.radarr_on else None,
                sonarr=None,
            ), plex, radarr

    def run(self):
        with self.executor() as (executor, plex, radarr):
            report = executor.run_deletions(passed_ids=self.passed)
            self.plex_deletes = [r["path"] for r in plex.requests if r["method"] == "DELETE"]
            self.radarr_writes = [(r["method"], r["path"]) for r in radarr.requests
                                  if r["method"] in WRITES]
        return report

    def row(self, request):
        return self.conn.execute("SELECT * FROM removal_requests WHERE id = ?",
                                 (request.id,)).fetchone()

    def events(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT outcome, unit, version_label, freed_bytes, error FROM removal_events"
            " WHERE outcome != 'cancelled' ORDER BY id")]


def test_one_version_is_removed_through_plex_and_the_other_stays(conn, tmp_path):
    world = World(conn, tmp_path)
    request = world.mark_hd()

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [("deleted", None)]
    assert world.plex_deletes == ["/library/metadata/11/media/502"]
    assert not world.hd.exists()
    assert world.four_k.exists()
    row = world.row(request)
    assert row["state"] == "deleted" and row["deleted_at"] is not None
    assert world.events() == [{"outcome": "deleted", "unit": "version",
                               "version_label": "1080 · h264", "freed_bytes": 12,
                               "error": None}]


def test_radarr_deleting_movies_does_not_route_a_version_to_radarr(conn, tmp_path):
    world = World(conn, tmp_path, radarr=True, radarr_deletes=True)
    world.mark_hd()

    report = world.run()

    assert [o.status for o in report.outcomes] == ["deleted"]
    assert world.radarr_writes == []
    assert world.plex_deletes == ["/library/metadata/11/media/502"]
    assert world.four_k.exists() and not world.hd.exists()


def test_the_file_radarr_manages_is_never_removed(conn, tmp_path):
    world = World(conn, tmp_path, radarr=True, managed="1080")
    request = world.mark_hd()

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.radarr_managed_version")]
    assert world.plex_deletes == [] and world.radarr_writes == []
    assert world.hd.exists() and world.four_k.exists()
    assert world.row(request)["state"] == "removal_failed"


def test_radarr_that_cannot_say_which_file_it_manages_refuses(conn, tmp_path):
    world = World(conn, tmp_path, radarr=True)
    world.radarr.fail_always("GET /api/v3/movie/1", 503)
    world.mark_hd()

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.radarr_identity")]
    assert world.plex_deletes == [] and world.radarr_writes == []
    assert world.hd.exists()


def test_the_dry_run_previews_one_file_and_sends_nothing(conn, tmp_path):
    world = World(conn, tmp_path, dry_run=True)
    request = world.mark_hd()
    before = dict(world.row(request))

    report = world.run()

    [outcome] = report.outcomes
    assert (outcome.status, outcome.files, outcome.size_bytes) == (
        "would_delete", (str(world.hd),), 12)
    assert world.plex_deletes == []
    assert world.hd.exists() and world.four_k.exists()
    assert dict(world.row(request)) == before


def test_the_kill_switch_blocks_a_version_removal(conn, tmp_path):
    world = World(conn, tmp_path)
    request = world.mark_hd()
    set_kill_switch(conn, True)

    report = world.run()

    assert [o.status for o in report.outcomes] == ["blocked_by_kill_switch"]
    assert world.plex_deletes == []
    assert world.hd.exists()
    assert world.row(request)["state"] == "recheck_passed"


def test_a_version_plex_changed_is_refused_and_nothing_is_sent(conn, tmp_path):
    world = World(conn, tmp_path)
    world.mark_hd()
    world.plex.metadata["11"]["Media"][1]["Part"][0]["file"] = "/data/Heat other.mkv"

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.plex_version_changed")]
    assert world.plex_deletes == []
    assert world.hd.exists()


def test_a_file_still_on_disk_after_plex_answered_is_not_deleted(conn, tmp_path):
    world = World(conn, tmp_path)
    request = world.mark_hd()
    world.plex.on_delete_version = lambda key, media_id: None

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.files_present")]
    assert world.row(request)["deleted_at"] is None


def test_a_kept_version_that_disappears_is_reported_never_claimed(conn, tmp_path):
    world = World(conn, tmp_path)
    request = world.mark_hd()

    def both(key, media_id):
        world.hd.unlink(missing_ok=True)
        world.four_k.unlink(missing_ok=True)

    world.plex.on_delete_version = both

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.kept_version_missing")]
    assert world.row(request)["deleted_at"] is None


def test_a_kept_version_already_missing_is_refused_before_anything_is_sent(conn, tmp_path):
    world = World(conn, tmp_path)
    world.mark_hd()
    world.four_k.unlink()

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.kept_version_missing")]
    assert world.plex_deletes == []
    assert world.hd.exists()


@pytest.mark.parametrize("code", [
    "plex_version_changed", "plex_version_parts", "radarr_managed_version",
    "kept_version_missing"])
def test_every_new_failure_reads_in_every_language(code):
    from almali_library_curator.i18n import translate

    for locale in ("en", "ca", "es"):
        key = f"removal.failed.{code}"
        assert translate(key, locale) != key, (locale, code)


def test_a_radarr_file_that_matches_no_stored_version_refuses(conn, tmp_path):
    # Verifier finding on 02c5a45: a Radarr path outside every path map stayed
    # unmapped, so the "is it Radarr's file" check could never match and the
    # refusal failed open. A managed file the app cannot place is a refusal.
    world = World(conn, tmp_path, radarr=True)
    world.radarr.movies[0]["movieFile"] = {"id": 9, "path": "/radarr-only/Heat 1080p.mkv"}
    world.mark_hd()

    report = world.run()

    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.radarr_identity")]
    assert world.plex_deletes == [] and world.radarr_writes == []
    assert world.hd.exists()
