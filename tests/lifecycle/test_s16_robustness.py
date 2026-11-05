"""S16 robustness of the removal scope and the stored failure text.

Rule 1: a damaged scope carrier never breaks a bulk read of requests.
Rule 2: a seasons scope with missing season rows is never the series
scope and deletes nothing.
Rule 3: the stored failure is a translation key, so the reader's
language decides the wording.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from almali_library_curator.detection.models import Unit
from almali_library_curator.i18n import translate
from almali_library_curator.lifecycle import DuplicateMarkError, LifecycleEngine
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from fake_radarr_server import FakeRadarrDataset, FakeRadarrServer, movie_resource
from fake_sonarr_server import FakeSonarrDataset, FakeSonarrServer
from lifecycle_helpers import (
    FakeCollections, FrozenClock, default_settings, make_candidate,
    seed_episode, seed_item, seed_library,
)


class SeasonsScenario:
    """A marked seasons scope (S1) over a series that also holds S2."""

    def __init__(self, conn, tmp_path, *, dry_run=False):
        self.conn = conn
        self.clock = FrozenClock()
        self.collections = FakeCollections()
        self.settings = default_settings(dry_run=dry_run)
        self.engine = LifecycleEngine(conn, settings=self.settings, clock=self.clock,
                                      collections=self.collections)
        self.library = seed_library(conn, "1", "Library")
        self.series = seed_item(conn, self.library, "item-1", "Foundation",
                                item_type="series")
        conn.execute("UPDATE media_items SET sonarr_id = 1 WHERE id = ?", (self.series,))
        self.sonarr = FakeSonarrDataset()
        self.passed_ids = []
        self.files = {}
        for season in (1, 2):
            seed_item(conn, self.library, f"item-1-s{season}", f"Season {season}",
                      item_type="season", parent_rating_key="item-1", season_number=season)
            key = seed_episode(conn, "item-1", season, 1)
            path = tmp_path / f"s{season}.mkv"
            path.write_bytes(b"x" * 17)
            self.files[season] = path
            conn.execute("UPDATE episodes SET file_path = ? WHERE plex_rating_key = ?",
                         (str(path), key))
            self.sonarr.episode_files.append(
                {"id": season, "seriesId": 1, "seasonNumber": season})
        conn.commit()

    def mark_season_one(self):
        request = self.engine.mark_for_removal(
            make_candidate(self.series, "item-1", "Foundation S1",
                           unit=Unit.SEASONS, seasons=(1,))
        )
        self.clock.advance(days=14)
        report = self.engine.run_recheck()
        self.passed_ids = [outcome.request.id for outcome in report.passed]
        return request

    def lose_season_rows(self):
        """The scope's season rows disappear after the mark (S16 rule 2)."""
        self.conn.execute("DELETE FROM episodes WHERE series_rating_key = 'item-1'"
                          " AND season_number = 1")
        self.conn.execute("DELETE FROM media_items WHERE plex_rating_key = 'item-1-s1'")
        self.conn.commit()

    def row(self, request):
        return self.conn.execute("SELECT * FROM removal_requests WHERE id = ?",
                                 (request.id,)).fetchone()

    @contextmanager
    def executor(self):
        with FakeSonarrServer(self.sonarr) as server:
            yield DeletionExecutor(
                self.engine,
                sonarr=SonarrClient(server.base_url, self.sonarr.api_key,
                                    rate_limit_per_second=0, max_retries=0),
            )


# -- rule 1: a damaged scope carrier never breaks a bulk read -----------------


@pytest.mark.parametrize("damage", ["json", "missing_row"])
def test_open_requests_skips_a_damaged_carrier_and_keeps_its_state(conn, damage):
    engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                             collections=FakeCollections())
    library = seed_library(conn, "1", "Library")
    first = seed_item(conn, library, "item-1", "Tenet")
    second = seed_item(conn, library, "item-2", "Arrival")
    damaged = engine.mark_for_removal(make_candidate(first, "item-1", "Tenet"))
    engine.mark_for_removal(make_candidate(second, "item-2", "Arrival"))
    if damage == "json":
        conn.execute("UPDATE candidates SET facts = 'broken-json' WHERE media_item_id = ?",
                     (first,))
    else:
        conn.execute("DELETE FROM candidates WHERE media_item_id = ?", (first,))
    conn.commit()

    open_requests = engine.open_requests()  # S16 rule 1: it must not raise.

    assert [request.media_item_id for request in open_requests] == [second]
    assert [request.state.value for request in open_requests] == ["marked"]
    # The damaged request keeps its state and is retried by the next scan.
    assert conn.execute("SELECT state FROM removal_requests WHERE id = ?",
                        (damaged.id,)).fetchone()["state"] == "marked"


def test_a_damaged_carrier_does_not_block_marking_another_item(conn):
    engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                             collections=FakeCollections())
    library = seed_library(conn, "1", "Library")
    first = seed_item(conn, library, "item-1", "Tenet")
    second = seed_item(conn, library, "item-2", "Arrival")
    engine.mark_for_removal(make_candidate(first, "item-1", "Tenet"))
    conn.execute("UPDATE candidates SET facts = 'broken-json' WHERE media_item_id = ?",
                 (first,))
    conn.commit()

    request = engine.mark_for_removal(make_candidate(second, "item-2", "Arrival"))

    assert request.media_item_id == second


@pytest.mark.parametrize("damage", ["json", "missing_row"])
def test_an_unreadable_scope_still_blocks_duplicate_marks_without_mutation(conn, damage):
    collections = FakeCollections()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                             collections=collections)
    library = seed_library(conn, "1", "Library")
    item = seed_item(conn, library, "item-1", "Tenet")
    candidate = make_candidate(item, "item-1", "Tenet")
    engine.mark_for_removal(candidate)
    if damage == "json":
        conn.execute("UPDATE candidates SET facts = 'broken-json' WHERE media_item_id = ?",
                     (item,))
    else:
        conn.execute("DELETE FROM candidates WHERE media_item_id = ?", (item,))
    conn.commit()
    before = list(conn.iterdump())
    writes = (list(collections.ensure_calls), collections.adds, collections.creates)

    with pytest.raises(DuplicateMarkError):
        engine.mark_for_removal(candidate)

    assert list(conn.iterdump()) == before
    assert (collections.ensure_calls, collections.adds, collections.creates) == writes
    # Reads still isolate the unreadable row instead of breaking a screen.
    assert engine.request_for(candidate) is None
    assert engine.open_requests() == []


# -- rule 2: a seasons scope never falls back to the series --------------------


def test_scope_keys_of_a_seasons_scope_never_collapse_to_the_series(conn):
    engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                             collections=FakeCollections())
    library = seed_library(conn, "1", "Library")
    series = seed_item(conn, library, "item-1", "Foundation", item_type="series")
    for season in (1, 2):
        seed_item(conn, library, f"item-1-s{season}", f"Season {season}",
                  item_type="season", parent_rating_key="item-1", season_number=season)
        seed_episode(conn, "item-1", season, 1)
    conn.commit()
    request = engine.mark_for_removal(
        make_candidate(series, "item-1", "Foundation S1", unit=Unit.SEASONS, seasons=(1,))
    )

    # The DELETION scope is the season's own row, never the series
    # container (S16 rule 2). The watch scope is a different set and
    # keeps the series key, so a history entry recorded against the show
    # still cancels the removal (see test_recheck).
    assert engine._scope_collection_keys(series, Unit.SEASONS, (1,)) == {
        "item-1-s1"
    }
    assert "item-1" not in engine._scope_collection_keys(
        series, Unit.SEASONS, (1,)
    )


def test_a_seasons_scope_with_missing_season_rows_never_occupies_the_series_row(conn):
    collections = FakeCollections()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=FrozenClock(),
                             collections=collections)
    library = seed_library(conn, "1", "Library")
    series = seed_item(conn, library, "item-1", "Foundation", item_type="series")
    # S2 has a season row; the scope is the missing S1, so the series row
    # itself must never be added to the collection as a stand-in.
    seed_item(conn, library, "item-1-s2", "Season 2", item_type="season",
              parent_rating_key="item-1", season_number=2)
    seed_episode(conn, "item-1", 2, 1)
    conn.commit()

    request = engine.mark_for_removal(
        make_candidate(series, "item-1", "Foundation S1", unit=Unit.SEASONS, seasons=(1,))
    )

    assert not collections.holds("item-1")
    assert request.collection_members == ()
    assert engine._request_by_id(request.id).collection_added is False


def test_a_seasons_scope_with_missing_season_rows_fails_with_its_own_reason(conn, tmp_path):
    scenario = SeasonsScenario(conn, tmp_path)
    request = scenario.mark_season_one()
    scenario.lose_season_rows()
    with scenario.executor() as executor:
        report = executor.run_deletions(passed_ids=scenario.passed_ids)

    outcome = report.outcomes[0]
    assert outcome.status.value == "failed"
    assert outcome.error == "removal.failed.seasons_missing"
    assert scenario.row(request)["state"] == "removal_failed"
    assert scenario.row(request)["last_error"] == "removal.failed.seasons_missing"
    event = conn.execute("SELECT * FROM removal_events").fetchone()
    assert event["outcome"] == "failed"
    assert event["error"] == "removal.failed.seasons_missing"
    # No backend is asked anything, and nothing is deleted: neither the
    # scope's file nor the untouched other season's.
    assert scenario.files[1].is_file() and scenario.files[2].is_file()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("damage", ["season_rows", "episode_rows", "both"])
def test_a_partly_missing_scope_never_reaches_the_backend(conn, tmp_path, damage, dry_run):
    scenario = SeasonsScenario(conn, tmp_path, dry_run=dry_run)
    request = scenario.engine.mark_for_removal(
        make_candidate(scenario.series, "item-1", "Foundation S1–S2",
                       unit=Unit.SEASONS, seasons=(1, 2))
    )
    scenario.clock.advance(days=14)
    passed = [outcome.request.id for outcome in scenario.engine.run_recheck().passed]
    if damage in ("season_rows", "both"):
        conn.execute("DELETE FROM media_items WHERE plex_rating_key = 'item-1-s1'")
    if damage in ("episode_rows", "both"):
        conn.execute("DELETE FROM episodes WHERE series_rating_key = 'item-1'"
                     " AND season_number = 1")
    conn.commit()
    before = list(conn.iterdump())

    with FakeSonarrServer(scenario.sonarr) as server:
        executor = DeletionExecutor(
            scenario.engine,
            sonarr=SonarrClient(server.base_url, scenario.sonarr.api_key,
                                rate_limit_per_second=0, max_retries=0),
        )
        outcome = executor.run_deletions(passed_ids=passed).outcomes[0]
        assert server.requests == []

    assert outcome.status.value == "failed"
    assert outcome.error == "removal.failed.seasons_missing"
    assert outcome.freed_bytes == 0
    assert all(path.read_bytes() == b"x" * 17 for path in scenario.files.values())
    if dry_run:
        assert list(conn.iterdump()) == before
        assert scenario.row(request)["state"] == "marked"
    else:
        assert scenario.row(request)["state"] == "removal_failed"
        assert scenario.row(request)["last_error"] == "removal.failed.seasons_missing"
        events = conn.execute("SELECT outcome, error, freed_bytes FROM removal_events").fetchall()
        assert [tuple(row) for row in events] == [
            ("failed", "removal.failed.seasons_missing", 0)
        ]


def test_dry_run_reports_the_missing_seasons_scope_without_mutating(conn, tmp_path):
    scenario = SeasonsScenario(conn, tmp_path, dry_run=True)
    request = scenario.mark_season_one()
    scenario.lose_season_rows()
    before = list(conn.iterdump())
    with scenario.executor() as executor:
        outcome = executor.run_deletions(passed_ids=scenario.passed_ids).outcomes[0]

    assert outcome.status.value == "failed"
    assert outcome.error == "removal.failed.seasons_missing"
    assert list(conn.iterdump()) == before
    assert scenario.row(request)["state"] == "marked"
    assert scenario.files[1].is_file() and scenario.files[2].is_file()


# -- rule 3: the stored failure is a translation key ---------------------------


def test_the_stored_failure_is_a_key_not_a_sentence(conn, tmp_path):
    clock = FrozenClock()
    engine = LifecycleEngine(conn, settings=default_settings(), clock=clock,
                             collections=FakeCollections())
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "movie-1", "Tenet")
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 17)
    conn.execute("UPDATE media_items SET file_path = ?, radarr_id = 1 WHERE id = ?",
                 (str(path), item))
    conn.commit()
    request = engine.mark_for_removal(make_candidate(item, "movie-1", "Tenet"))
    clock.advance(days=14)
    passed = [outcome.request.id for outcome in engine.run_recheck().passed]

    radarr = FakeRadarrDataset()
    radarr.movies.append(movie_resource(1, "Tenet"))
    with FakeRadarrServer(radarr) as server:
        address = server.base_url
        server.stop()
        client = RadarrClient(address, radarr.api_key,
                              rate_limit_per_second=0, max_retries=0)
        outcome = DeletionExecutor(engine, radarr=client).run_deletions(
            passed_ids=passed).outcomes[0]

    stored = conn.execute("SELECT last_error FROM removal_requests WHERE id = ?",
                          (request.id,)).fetchone()["last_error"]
    assert outcome.status.value == "failed"
    assert stored == "removal.failed.service"
    assert outcome.error == stored
    event = conn.execute("SELECT error FROM removal_events").fetchone()
    assert event["error"] == stored
    # The reader's language decides the wording, and every locale renders
    # the key as a full sentence without placeholders.
    for locale in ("en", "ca", "es"):
        assert "{" not in translate(stored, locale)
    assert len({translate(stored, locale) for locale in ("en", "ca", "es")}) == 3
    assert conn.execute("SELECT last_error FROM removal_requests WHERE id = ?",
                        (request.id,)).fetchone()["last_error"] == "removal.failed.service"
