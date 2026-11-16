"""#47 S1c/S1d: Leaving soon, History and the CSV export render the stored
reasons in the language the page asks for, and rows written before the
reasons carried parts keep their stored text."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json

from markupsafe import escape
import pytest

from almali_library_curator.detection import text
from almali_library_curator.detection.models import Candidate, Category, Match, Unit
from almali_library_curator.i18n import SUPPORTED_LOCALES, translate
from almali_library_curator.lifecycle import LifecycleEngine
from lifecycle_helpers import FrozenClock, seed_item, seed_library, seed_play, seed_user

# The request is marked a few days ago, so the cancellation is still listed
# as recent on Leaving soon whatever day the suite runs.
MARKED_AT = datetime.now(timezone.utc) - timedelta(days=3)
PLAYED_AT = MARKED_AT + timedelta(minutes=1)

DETECTION_DATE = "2024-01-05T12:00:00+00:00"
DETECTION_PARTS = (
    text.reason_part("detection.reason.never_watched.movie", date=DETECTION_DATE, users=3),
)

CANCEL_SENTENCE = {
    locale: text.render(
        locale,
        "removal.cancel.started_watching",
        name="Marc",
        date=text.day_month(locale, PLAYED_AT),
    )
    for locale in SUPPORTED_LOCALES
}
DETECTION_SENTENCE = {
    locale: text.render_reason(locale, DETECTION_PARTS) for locale in SUPPORTED_LOCALES
}

# The stored text of a row written before the parts existed: scan-time
# English, which a Catalan or Spanish page must not "translate" away (S1d).
LEGACY_CANCEL_TEXT = "Removal cancelled: Marc started watching on 5 January."


def mark(conn, app):
    """A real request marked with the app's own settings, in English."""
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
    clock = FrozenClock(MARKED_AT)
    # A live lifecycle: the preview only reports, it never records (S5).
    settings = replace(app.config["SETTINGS"], dry_run=False)
    engine = LifecycleEngine(conn, settings=settings, clock=clock)
    # The shortest grace period, so the re-check is due on the same day.
    return engine, clock, engine.mark_for_removal(candidate, grace_days=3)


def cancelled_by_a_play(conn, app):
    """A request marked in English and cancelled by a later play."""
    engine, clock, request = mark(conn, app)
    seed_user(conn, "1", "Marc")
    seed_play(conn, 1, "11", PLAYED_AT)
    clock.advance(days=3)
    engine.run_recheck()
    return request


def shown(page, sentence):
    """The sentence as the page's HTML escaping renders it."""
    return str(escape(sentence)) in page


def csv_rows(client, locale, outcome=None):
    query = f"lang={locale}" + (f"&outcome={outcome}" if outcome else "")
    response = client.get(f"/history.csv?{query}")
    assert response.status_code == 200
    return list(
        csv.DictReader(io.StringIO(response.get_data(as_text=True), newline=""))
    )


@pytest.mark.parametrize("locale", ["ca", "es"])
def test_leaving_soon_renders_a_cancellation_reason_in_the_language_asked(
    client, conn, app, locale
):
    cancelled_by_a_play(conn, app)

    page = client.get(f"/?tab=leaving_soon&lang={locale}").get_data(as_text=True)

    assert shown(page, CANCEL_SENTENCE[locale]), CANCEL_SENTENCE[locale]
    assert not shown(page, CANCEL_SENTENCE["en"])


@pytest.mark.parametrize("locale", ["ca", "es"])
@pytest.mark.parametrize(
    "reason, sentence",
    [
        pytest.param("cancel", CANCEL_SENTENCE, id="cancellation"),
        pytest.param("detection", DETECTION_SENTENCE, id="detection"),
    ],
)
def test_history_and_csv_render_the_reason_in_the_language_asked(
    client, conn, app, locale, reason, sentence
):
    if reason == "cancel":
        cancelled_by_a_play(conn, app)
    else:
        seed_deleted_event(conn)

    page = client.get(f"/history?lang={locale}").get_data(as_text=True)
    [row] = csv_rows(client, locale)

    assert shown(page, sentence[locale]), sentence[locale]
    # The CSV header is localised (S1, #73): the reason cell keeps its column.
    assert row[translate("csv.event.reason", locale)] == sentence[locale]
    assert not shown(page, sentence["en"]) or locale == "en"


@pytest.mark.parametrize("locale", ["ca", "es"])
def test_rows_written_before_the_parts_keep_their_stored_text(
    client, conn, app, locale
):
    request = cancelled_by_a_play(conn, app)
    # A pre-migration row: text only, no parts and no raw parameters.
    conn.execute(
        "UPDATE removal_requests SET cancel_reason = ?, cancel_params = NULL,"
        " reason_parts = NULL WHERE id = ?",
        (LEGACY_CANCEL_TEXT, request.id),
    )
    seed_deleted_event(conn)
    conn.execute(
        "UPDATE removal_events SET reason = ?, reason_parts = NULL"
        " WHERE outcome = 'deleted'",
        (DETECTION_SENTENCE["en"],),
    )
    conn.commit()

    leaving = client.get(f"/?tab=leaving_soon&lang={locale}").get_data(as_text=True)
    history = client.get(f"/history?lang={locale}").get_data(as_text=True)
    [row] = csv_rows(client, locale, outcome="deleted")

    assert shown(leaving, LEGACY_CANCEL_TEXT)
    assert shown(history, DETECTION_SENTENCE["en"])
    assert row[translate("csv.event.reason", locale)] == DETECTION_SENTENCE["en"]
    assert not shown(history, DETECTION_SENTENCE[locale])


def seed_deleted_event(conn):
    """A deleted event as the deletion stage writes it, parts included."""
    conn.execute(
        "INSERT INTO removal_events (outcome, title, unit, category, reason,"
        " reason_parts, occurred_at) VALUES ('deleted', 'Tenet', 'movie',"
        " 'never_watched', ?, ?, ?)",
        (
            DETECTION_SENTENCE["en"],
            json.dumps(list(DETECTION_PARTS)),
            "2026-11-01T12:00:00+00:00",
        ),
    )
    conn.commit()


def test_a_damaged_parts_record_falls_back_to_the_stored_text(client, conn, app):
    """An unreadable record never leaves a row without its reason (S1d)."""
    seed_deleted_event(conn)
    conn.execute("UPDATE removal_events SET reason_parts = 'not-json'")
    conn.commit()

    history = client.get("/history?lang=ca").get_data(as_text=True)
    [row] = csv_rows(client, "ca", outcome="deleted")

    assert shown(history, DETECTION_SENTENCE["en"])
    assert row[translate("csv.event.reason", "ca")] == DETECTION_SENTENCE["en"]
