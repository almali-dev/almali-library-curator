"""#47 S1a/S1c/S1d: a reason is stored as a key plus raw parameters.

The rendered text stays beside the parts, so rows written before them and a
downgrade keep reading, and the raw parameters stay raw (ISO dates, names,
counts) until a screen renders them in the reader's language.
"""

from __future__ import annotations

from datetime import timedelta
import json

from lifecycle_helpers import (
    FakeCollections,
    FrozenClock,
    default_settings,
    seed_item,
    seed_library,
    seed_play,
    seed_user,
)

from almali_library_curator.detection import text
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from almali_library_curator.lifecycle import CancelCode, LifecycleEngine, RemovalState

# The raw parameters a detection reason carries: the date stays an ISO string.
DETECTION_PARAMS = {"date": "2024-01-05T12:00:00+00:00", "users": 3}
DETECTION_PARTS = (
    text.reason_part("detection.reason.never_watched.movie", **DETECTION_PARAMS),
)


def marked_request(conn, *, locale: str = "en"):
    """A real request marked over a real seeded row, with one reason part."""
    settings = default_settings(locale=locale)
    clock = FrozenClock()
    library = seed_library(conn, "1", "Movies")
    item = seed_item(conn, library, "11", "Tenet")
    candidate = Candidate(
        media_item_id=item,
        plex_rating_key="11",
        title="Tenet",
        unit=Unit.MOVIE,
        seasons=(),
        matches=(
            Match(
                Category.NEVER_WATCHED,
                text.render_reason("en", DETECTION_PARTS),
                DETECTION_PARTS,
            ),
        ),
        size_bytes=1,
        rating=None,
        facts={"matches": {"never_watched": {}}},
    )
    engine = LifecycleEngine(
        conn, settings=settings, clock=clock, collections=FakeCollections()
    )
    return engine, clock, engine.mark_for_removal(candidate, grace_days=3)


def stored_parts(value: str | None):
    return json.loads(value) if value is not None else None


def test_marking_stores_the_detection_reason_parts_beside_the_text(conn):
    engine, _, request = marked_request(conn)

    row = conn.execute(
        "SELECT reason, reason_parts, cancel_params FROM removal_requests"
    ).fetchone()

    assert row["reason"] == text.render_reason("en", DETECTION_PARTS)
    assert stored_parts(row["reason_parts"]) == list(DETECTION_PARTS)
    # Nothing was cancelled yet, so there are no cancel parameters.
    assert row["cancel_params"] is None
    assert request.state is RemovalState.MARKED


def test_a_started_watching_cancel_stores_raw_params_and_its_reason_part(conn):
    engine, clock, request = marked_request(conn)
    seed_user(conn, "1", "Marc")
    clock.advance(minutes=5)
    played_at = clock.now - timedelta(minutes=4)
    seed_play(conn, 1, "11", played_at)
    clock.advance(days=3)

    report = engine.run_recheck()

    [outcome] = report.cancelled
    assert outcome.exit_code is CancelCode.STARTED_WATCHING
    row = conn.execute(
        "SELECT state, recheck_status, cancel_reason, cancel_params"
        " FROM removal_requests WHERE id = ?",
        (request.id,),
    ).fetchone()
    assert row["state"] == RemovalState.CANCELLED.value
    assert row["recheck_status"] == "started_watching"
    # The stored date is raw (ISO), never the scan language's words.
    assert stored_parts(row["cancel_params"]) == {
        "name": "Marc",
        "date": played_at.isoformat(),
    }
    assert row["cancel_reason"] == text.render(
        "en",
        "removal.cancel.started_watching",
        name="Marc",
        date=text.day_month("en", played_at),
    )
    # History keeps the same reason as a key plus the same raw parameters.
    event = conn.execute(
        "SELECT reason, reason_parts FROM removal_events WHERE outcome = 'cancelled'"
    ).fetchone()
    assert event["reason"] == row["cancel_reason"]
    assert stored_parts(event["reason_parts"]) == [
        {
            "key": "removal.cancel.started_watching",
            "params": {"name": "Marc", "date": played_at.isoformat()},
        }
    ]
    # The model reads the stored parameters back as raw values.
    reloaded = engine._request_by_id(request.id)
    assert reloaded.cancel_params == {
        "name": "Marc",
        "date": played_at.isoformat(),
    }


def test_a_cancel_that_needs_no_parameters_stores_an_empty_object(conn):
    engine, _, request = marked_request(conn)

    engine.keep_it(request)

    row = conn.execute(
        "SELECT recheck_status, cancel_params FROM removal_requests WHERE id = ?",
        (request.id,),
    ).fetchone()
    assert row["recheck_status"] == "keep_it"
    # An empty object, not NULL: NULL is what a pre-migration row holds.
    assert row["cancel_params"] == "{}"
    event = conn.execute(
        "SELECT reason_parts FROM removal_events WHERE outcome = 'cancelled'"
    ).fetchone()
    assert stored_parts(event["reason_parts"]) == [
        {"key": "removal.cancel.keep_it", "params": {}}
    ]


def test_a_deleted_event_copies_the_detection_reason_parts(conn, tmp_path):
    from test_deletion import Scenario

    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    conn.execute(
        "UPDATE removal_requests SET reason_parts = ? WHERE id = ?",
        (json.dumps(list(DETECTION_PARTS)), request.id),
    )
    conn.commit()

    with scenario.executor() as (executor, _radarr, _sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)

    assert [outcome.status for outcome in report.outcomes] == ["deleted"]
    event = conn.execute(
        "SELECT reason, reason_parts FROM removal_events WHERE outcome = 'deleted'"
    ).fetchone()
    assert stored_parts(event["reason_parts"]) == list(DETECTION_PARTS)
    assert event["reason"] == scenario.row(request)["reason"]
