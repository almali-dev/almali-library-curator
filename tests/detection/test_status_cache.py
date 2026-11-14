"""A reused engine asks the series status again every pass (#34).

The series-status answers are scoped to one detection pass, exactly like
the users query: within a pass the enricher is asked once per series, and
a reused engine sees the source's new answer on its next pass.
"""

import pytest

from fake_plex_server import FakePlexDataset

from scenarios import (
    NOW,
    FixedSeriesStatus,
    add_series,
    default_settings,
    full_play,
    sync_dataset,
    ts,
)

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.detection.enrichers import Enrichers
from almali_library_curator.detection.models import Category
from almali_library_curator.protection import ProtectionEngine

METHODS = ["scan", "scan_with_protection", "scan_items"]


def cancelled_dataset(title: str, rating_key: str) -> FakePlexDataset:
    data = FakePlexDataset()
    add_series(data, rating_key, title, {1: [2, 2]}, addedAt=ts(2022, 1, 10))
    data.history = [
        full_play(f"{rating_key}-1-1", 1, ts(2023, 8, 10)),
        full_play(f"{rating_key}-1-2", 1, ts(2023, 8, 15)),
    ]
    return data


def build_engine(conn, *, enricher=None, with_protector=False) -> DetectionEngine:
    settings = default_settings()
    return DetectionEngine(
        conn,
        enrichers=Enrichers(series_status=enricher) if enricher is not None else None,
        clock=lambda: NOW,
        settings=settings,
        protector=ProtectionEngine(conn, settings=settings) if with_protector else None,
    )


def run_pass(engine, method):
    """One public detection pass, answering its candidates."""
    if method == "scan_items":
        ids = [
            int(row["id"])
            for row in engine.conn.execute(
                "SELECT id FROM media_items WHERE item_type = 'series'"
            )
        ]
        return list(engine.scan_items(ids).candidates)
    if method == "scan_with_protection":
        return list(engine.scan_with_protection().candidates)
    return engine.scan()


@pytest.mark.parametrize("with_protector", [False, True])
@pytest.mark.parametrize("method", METHODS)
def test_second_pass_asks_the_series_status_again(conn, method, with_protector):
    """The stale cached status must not decide the next pass."""
    sync_dataset(conn, cancelled_dataset("Westworld", "400"))
    enricher = FixedSeriesStatus("cancelled", ended_year=2022)
    engine = build_engine(conn, enricher=enricher, with_protector=with_protector)

    first = run_pass(engine, method)
    assert len(first) == 1
    assert first[0].categories == (Category.CANCELLED_NO_VIEWERS,)
    assert first[0].facts["matches"]["cancelled_no_viewers"]["status"] == "cancelled"
    assert enricher.calls == [("Westworld", "400")]

    enricher.status = "continuing"

    second = run_pass(engine, method)
    assert second == []
    assert enricher.calls == [("Westworld", "400"), ("Westworld", "400")]


@pytest.mark.parametrize("method", METHODS)
def test_second_pass_sees_a_newly_cancelled_series(conn, method):
    """The fresh answer must reach the pass in both directions, and the
    first pass still answers with the status the enricher gave."""
    sync_dataset(conn, cancelled_dataset("Dead to Me", "410"))
    enricher = FixedSeriesStatus("continuing")
    engine = build_engine(conn, enricher=enricher)

    assert run_pass(engine, method) == []

    enricher.status = "cancelled"
    enricher.ended_year = 2024

    second = run_pass(engine, method)
    assert len(second) == 1
    assert second[0].categories == (Category.CANCELLED_NO_VIEWERS,)
    assert second[0].facts["matches"]["cancelled_no_viewers"] == {
        "status": "cancelled",
        "ended_year": 2024,
        "last_played_at": "2023-08-15T12:00:00+00:00",
        "mid_show_users": [],
    }


@pytest.mark.parametrize("method", METHODS)
def test_one_enricher_call_per_series_within_a_pass(conn, method):
    """The per-pass dedup survives the reset, for every series."""
    data = FakePlexDataset()
    add_series(data, "400", "Westworld", {1: [2, 2]}, addedAt=ts(2022, 1, 10))
    add_series(data, "410", "Dead to Me", {1: [2, 2]}, addedAt=ts(2022, 1, 10))
    data.history = [
        full_play("400-1-1", 1, ts(2023, 8, 10)),
        full_play("400-1-2", 1, ts(2023, 8, 15)),
        full_play("410-1-1", 1, ts(2023, 8, 10)),
        full_play("410-1-2", 1, ts(2023, 8, 15)),
    ]
    sync_dataset(conn, data)
    enricher = FixedSeriesStatus("cancelled", ended_year=2022)
    engine = build_engine(conn, enricher=enricher)

    first = run_pass(engine, method)
    assert len(first) == 2
    assert sorted(enricher.calls) == [
        ("Dead to Me", "410"),
        ("Westworld", "400"),
    ]

    second = run_pass(engine, method)
    assert len(second) == 2
    assert len(enricher.calls) == 4
    for series in [("Dead to Me", "410"), ("Westworld", "400")]:
        assert enricher.calls.count(series) == 2


@pytest.mark.parametrize("method", METHODS)
def test_a_reused_engine_without_an_enricher_keeps_degrading(conn, method):
    """No enricher means no status answer, on every pass."""
    sync_dataset(conn, cancelled_dataset("Westworld", "400"))
    engine = build_engine(conn)

    assert run_pass(engine, method) == []
    assert run_pass(engine, method) == []
