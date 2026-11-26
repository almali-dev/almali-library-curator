"""Finished seasons of a running series: its own category and threshold (#135).

The category proposes only the seasons every follower finished, keeps the
latest finished season, and leaves ended, cancelled and stalled series, and
any series the seasons everyone finished category already proposes, to the
category that covers it.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from fake_plex_server import FakePlexDataset

from almali_library_curator.config import Thresholds
from almali_library_curator.detection import text
from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.protection import ProtectionEngine, ProtectionKind

from scenarios import (
    GB,
    NOW,
    FixedSeriesStatus,
    add_series,
    default_settings,
    full_play,
    partial_play,
    scan,
    sync_dataset,
    ts,
)

CATEGORY = "running_series_finished_seasons"
MONTHS = "running_series_finished_seasons_months"


def proposed(candidates):
    """The candidates the new category matched."""
    return [candidate for candidate in candidates if CATEGORY in candidate.categories]


def airing(data: FakePlexDataset, season_key: str, when: int) -> None:
    """Date a season's episodes, the way a season that is airing arrives."""
    for episode in data.children[season_key]:
        episode["addedAt"] = when


def running_dataset() -> FakePlexDataset:
    """S1–S3 finished by both users, S4 airing (its episodes arrived last month)."""
    data = FakePlexDataset()
    add_series(
        data,
        "600",
        "Foundation",
        {1: [1, 1], 2: [1, 1], 3: [1, 1], 4: [2, 2]},
        addedAt=ts(2024, 1, 5),
    )
    for account_id in (1, 2):
        for season, moments in (
            (1, (ts(2025, 1, 10), ts(2025, 1, 20))),
            (2, (ts(2025, 2, 10), ts(2025, 2, 20))),
            (3, (ts(2025, 3, 10), ts(2025, 3, 20))),
        ):
            for number, moment in enumerate(moments, start=1):
                data.history.append(
                    full_play(f"600-{season}-{number}", account_id, moment)
                )
    airing(data, "600-4", ts(2026, 10, 15))
    return data


def finished_run_dataset() -> FakePlexDataset:
    """S1–S3 finished by both users; S4 is airing on TV, not in the library yet."""
    data = running_dataset()
    data.children["600"] = [
        entry for entry in data.children["600"] if entry["ratingKey"] != "600-4"
    ]
    data.children.pop("600-4")
    return data


def test_a_running_series_proposes_the_finished_seasons_before_the_latest_one(conn):
    sync_dataset(conn, finished_run_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    (candidate,) = proposed(candidates)
    assert candidate.title == "Foundation S1–S2"
    assert candidate.unit.value == "seasons"
    assert candidate.categories == (CATEGORY,)
    assert candidate.seasons == (1, 2)
    assert candidate.size_bytes == 4 * GB
    assert candidate.reasons == (
        "Marc and Anna (the only followers) both finished S1–S2."
        " The series is still running: S3 is the latest finished season,"
        " so it stays with the newer ones.",
    )
    facts = candidate.facts["matches"][CATEGORY]
    assert facts["followers"] == ["Marc", "Anna"]
    assert facts["finished_seasons"] == [1, 2]
    assert facts["kept_season"] == 3
    assert facts["all_finished_at"] == "2025-02-20T12:00:00+00:00"
    assert facts["status"] == "continuing"


def test_a_season_someone_is_still_watching_is_never_proposed(conn):
    data = FakePlexDataset()
    add_series(
        data, "610", "Severance", {1: [1, 1], 2: [1, 1], 3: [1, 1]}, addedAt=ts(2024, 1, 5)
    )
    for account_id in (1, 2):
        for season, moments in (
            (1, (ts(2025, 1, 10), ts(2025, 1, 20))),
            (2, (ts(2025, 2, 10), ts(2025, 2, 20))),
        ):
            for number, moment in enumerate(moments, start=1):
                data.history.append(
                    full_play(f"610-{season}-{number}", account_id, moment)
                )
    # Anna is still watching S3: a partial play nothing else counts as done.
    data.history.append(partial_play("610-3-1", 2, ts(2026, 10, 20), 0.4))
    airing(data, "610-3", ts(2026, 10, 15))
    sync_dataset(conn, data)

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    # One season row for the series, from the category that already covers
    # the unfinished later season (L8), and S3 is not in it.
    (row,) = [row for row in candidates if row.unit.value == "seasons"]
    assert row.categories == ("seasons_everyone_finished",)
    assert row.seasons == (1, 2)
    assert 3 not in row.seasons


def test_a_series_with_a_single_finished_season_proposes_nothing(conn):
    """S2 is being watched, so there is nothing before the latest finished one."""
    data = FakePlexDataset()
    add_series(
        data, "620", "Silo", {1: [1, 1], 2: [1, 1]}, addedAt=ts(2024, 1, 5)
    )
    for account_id in (1, 2):
        data.history.extend(
            [
                full_play("620-1-1", account_id, ts(2025, 1, 10)),
                full_play("620-1-2", account_id, ts(2025, 1, 20)),
            ]
        )
    data.history.append(partial_play("620-2-1", 2, ts(2026, 10, 20), 0.5))
    airing(data, "620-2", ts(2026, 10, 15))
    sync_dataset(conn, data)

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    assert proposed(candidates) == []


def test_a_season_one_user_watched_under_90_percent_is_not_proposed(conn):
    data = FakePlexDataset()
    add_series(
        data, "640", "Dark", {1: [1, 1], 2: [1, 1]}, addedAt=ts(2024, 1, 5)
    )
    # Anna stopped inside S1's second episode: it is not watched past 90%.
    data.history = [
        full_play("640-1-1", 1, ts(2025, 1, 10)),
        partial_play("640-1-2", 2, ts(2025, 1, 20), 0.5),
        full_play("640-2-1", 1, ts(2025, 2, 10)),
        full_play("640-2-2", 2, ts(2025, 2, 20)),
    ]
    sync_dataset(conn, data)

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    assert proposed(candidates) == []


def test_the_threshold_settings_decide_when_the_category_matches(conn):
    """Finished last month: not yet; the same series matches with one month."""
    data = FakePlexDataset()
    add_series(
        data, "650", "Silo", {1: [1, 1], 2: [1, 1]}, addedAt=ts(2024, 1, 5)
    )
    for account_id in (1, 2):
        for season, moments in (
            (1, (ts(2026, 9, 20), ts(2026, 9, 25))),
            (2, (ts(2026, 9, 20), ts(2026, 9, 25))),
        ):
            for number, moment in enumerate(moments, start=1):
                data.history.append(
                    full_play(f"650-{season}-{number}", account_id, moment)
                )
    sync_dataset(conn, data)

    not_yet = scan(
        conn, settings=default_settings(), series_status=FixedSeriesStatus("continuing")
    )

    assert proposed(not_yet) == []

    soon = default_settings(
        thresholds=replace(Thresholds(), running_series_finished_seasons_months=1)
    )
    matched = scan(conn, settings=soon, series_status=FixedSeriesStatus("continuing"))

    (candidate,) = proposed(matched)
    assert candidate.seasons == (1,)


def test_a_protected_series_is_never_a_candidate(conn):
    settings = default_settings(protected_libraries=("Shows",))
    sync_dataset(conn, finished_run_dataset())
    protector = ProtectionEngine(conn, settings=settings)
    engine = DetectionEngine(
        conn,
        clock=lambda: NOW,
        settings=settings,
        enrichers=None,
        protector=protector,
    )

    outcome = engine.scan_with_protection()

    assert proposed(outcome.candidates) == []
    protected = [
        entry for entry in outcome.protected if CATEGORY in entry.candidate.categories
    ]
    (entry,) = protected
    assert entry.protection.kind is ProtectionKind.LIBRARY
    assert entry.candidate.seasons == (1, 2)


def test_an_ended_series_is_left_to_the_existing_categories(conn):
    sync_dataset(conn, finished_run_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("ended", ended_year=2026))

    assert proposed(candidates) == []
    assert any(
        "cancelled_no_viewers" in candidate.categories for candidate in candidates
    )


def test_a_cancelled_series_is_left_to_the_existing_categories(conn):
    sync_dataset(conn, finished_run_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("cancelled", ended_year=2026))

    assert proposed(candidates) == []


def test_a_stalled_series_is_left_to_the_stalled_category(conn):
    data = FakePlexDataset()
    add_series(
        data, "630", "Lost in Space", {1: [1, 1], 2: [1, 1], 3: [1, 1]},
        addedAt=ts(2023, 6, 1),
    )
    for account_id in (1, 2):
        for season, moments in (
            (1, (ts(2025, 1, 10), ts(2025, 1, 20))),
            (2, (ts(2025, 2, 10), ts(2025, 2, 20))),
        ):
            for number, moment in enumerate(moments, start=1):
                data.history.append(
                    full_play(f"630-{season}-{number}", account_id, moment)
                )
    sync_dataset(conn, data)

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    assert proposed(candidates) == []
    assert any("stalled_series" in candidate.categories for candidate in candidates)


def test_the_everyone_finished_category_owns_an_overlapping_series(conn):
    """L8: one season row per series, and it is the existing category's."""
    sync_dataset(conn, running_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    season_rows = [candidate for candidate in candidates if candidate.unit.value == "seasons"]
    (row,) = season_rows
    assert row.categories == ("seasons_everyone_finished",)
    assert row.seasons == (1, 2, 3)
    assert proposed(candidates) == []


def test_the_everyone_finished_category_keeps_its_own_behaviour(conn):
    """Reusing its finished-seasons computation changed nothing for it."""
    sync_dataset(conn, running_dataset())

    candidates = scan(conn, series_status=FixedSeriesStatus("continuing"))

    (candidate,) = [
        row for row in candidates if "seasons_everyone_finished" in row.categories
    ]
    assert candidate.title == "Foundation S1–S3"
    assert candidate.seasons == (1, 2, 3)
    assert candidate.reasons == (
        "Marc and Anna (the only followers) both finished S1–S3. S4 is in progress.",
    )


EXPECTED_REASONS = {
    "en": (
        "Marc and Anna (the only followers) both finished S1–S2."
        " The series is still running: S3 is the latest finished season,"
        " so it stays with the newer ones."
    ),
    "ca": (
        "Marc i Anna (els únics seguidors) tots dos han acabat S1–S2."
        " La sèrie encara s'emet: S3 és la temporada acabada més recent,"
        " així que es queda amb les més noves."
    ),
    "es": (
        "Marc y Anna (los únicos seguidores) han terminado S1–S2."
        " La serie sigue en emisión: S3 es la temporada terminada más reciente,"
        " así que se queda con las nuevas."
    ),
}


@pytest.mark.parametrize("locale", sorted(EXPECTED_REASONS))
def test_the_reason_renders_in_every_locale_from_its_parts(conn, locale):
    sync_dataset(conn, finished_run_dataset())

    candidates = scan(
        conn,
        settings=default_settings(locale=locale),
        series_status=FixedSeriesStatus("continuing"),
    )

    (candidate,) = proposed(candidates)
    (match,) = candidate.matches
    assert match.reason == EXPECTED_REASONS[locale]
    assert text.render_reason(locale, match.reason_parts) == match.reason
    stored = json.loads(json.dumps(candidate.reason_parts))
    assert text.render_reason(locale, stored) == match.reason
