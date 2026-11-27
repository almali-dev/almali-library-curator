"""S1 (#148): Leaving soon is one joined query and one grouped aggregate.

The screen used to build every open request with its own lookups — the scope
carrier, the item's display facts, its size and disk, and the files a removal
would delete — so the cost of a page grew with the number of marked items, and
up to 50 cancelled requests were built for a tab that never shows them. These
tests read the statements the page really ran, through the signed-in client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from markupsafe import Markup

from almali_library_curator.detection.models import Unit
from almali_library_curator.lifecycle import LifecycleEngine
from lifecycle_helpers import (
    FakeCollections, FrozenClock, make_candidate, seed_episode, seed_item, seed_library,
)

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
GB = 1024 ** 3


def engine_for(app, conn):
    return LifecycleEngine(conn, settings=app.config["SETTINGS"],
                           clock=FrozenClock(NOW), collections=FakeCollections())


def mark(engine, item_id, key, title, *, unit=Unit.MOVIE, seasons=()):
    """Mark one candidate through the engine's public call."""
    return engine.mark_for_removal(
        make_candidate(item_id, key, title, unit=unit, seasons=seasons), marked_by="owner")


def traced_page(client, conn, url):
    """A page rendered over ``conn``, with every statement it ran."""
    statements = []
    with patch("almali_library_curator.web.routes.get_db", return_value=conn):
        conn.set_trace_callback(statements.append)
        try:
            response = client.get(url)
        finally:
            conn.set_trace_callback(None)
    return response, statements


def page_text(response):
    return " ".join(Markup(response.get_data(as_text=True)).striptags().split())


def seed_movies(conn, engine, count, *, first=0, library=None):
    library = library or seed_library(conn, "1", "Movies")
    for index in range(first, first + count):
        item = seed_item(conn, library, f"10{index}", f"Movie {index}")
        mark(engine, item, f"10{index}", f"Movie {index}")
    return library


def seed_show(conn, engine, *, seasons=(1, 2, 3), episodes=2, mark_unit=Unit.SERIES,
              wanted=()):
    """A series with its season containers and episode rows, marked for removal."""
    library = seed_library(conn, "1", "TV Shows", section_type="show")
    series = seed_item(conn, library, "90", "Show", item_type="series")
    for season in seasons:
        seed_item(conn, library, f"9{season}", f"Season {season}", item_type="season",
                  parent_rating_key="90", season_number=season)
        for episode in range(1, episodes + 1):
            key = seed_episode(conn, "90", season, episode, size_bytes=GB)
            conn.execute("UPDATE episodes SET disk = ? WHERE plex_rating_key = ?",
                         (f"disk{season}", key))
    conn.commit()
    return series, mark(engine, series, "90", "Show", unit=mark_unit, seasons=wanted)


def attached(conn, app, *, tab="leaving_soon"):
    from almali_library_curator.web.views import library_view

    return library_view(conn, app.config["SETTINGS"], clock=lambda: NOW, tab=tab)


def test_the_page_does_not_run_more_statements_as_requests_grow(app, conn, client):
    engine = engine_for(app, conn)
    library = seed_movies(conn, engine, 3, first=1)
    _, few = traced_page(client, conn, "/?tab=leaving_soon")
    seed_movies(conn, engine, 27, first=4, library=library)
    response, many = traced_page(client, conn, "/?tab=leaving_soon")
    assert response.status_code == 200
    text = page_text(response)
    # Every request is still on the page, in its own row.
    assert all(f"Movie {index}" in text for index in range(1, 31))
    assert len(many) == len(few), (len(few), len(many))


def test_the_candidates_tab_costs_the_same_as_the_requests_grow(app, conn, client):
    engine = engine_for(app, conn)
    library = seed_movies(conn, engine, 3, first=1)
    _, few = traced_page(client, conn, "/")
    seed_movies(conn, engine, 27, first=4, library=library)
    response, many = traced_page(client, conn, "/")
    assert response.status_code == 200
    assert len(many) == len(few), (len(few), len(many))


def test_a_page_of_series_reads_their_episodes_once(app, conn, client):
    """S1: one grouped aggregate answers every row of the page."""
    engine = engine_for(app, conn)
    seed_show(conn, engine)
    response, statements = traced_page(client, conn, "/?tab=leaving_soon")
    assert response.status_code == 200
    aggregates = [statement for statement in statements
                  if "FROM episodes" in statement and "GROUP BY series_rating_key" in statement]
    assert len(aggregates) == 1, aggregates
    # No row reads its episodes on its own any more.
    per_row = [statement for statement in statements
               if "FROM episodes" in statement and "series_rating_key = ?" in statement
               and "GROUP BY" not in statement]
    assert per_row == []


def test_the_cancelled_requests_are_built_only_for_their_tab(app, conn, client):
    """S1: no tab pays for the cancelled list it never shows."""
    engine = engine_for(app, conn)
    library = seed_library(conn, "1", "Movies")
    kept = seed_item(conn, library, "70", "Kept Movie")
    engine.keep_it(mark(engine, kept, "70", "Kept Movie"))
    marked = seed_item(conn, library, "71", "Marked Movie")
    mark(engine, marked, "71", "Marked Movie")

    candidates = attached(conn, app, tab="candidates")
    assert candidates.leaving_soon.cancelled == ()
    leaving = attached(conn, app)
    assert [row.title for row in leaving.leaving_soon.cancelled] == ["Kept Movie"]
    # The counts every tab reads are the same on both.
    assert candidates.summary.leaving_soon == leaving.summary.leaving_soon
    assert (candidates.summary.leaving_soon.count,
            [row.title for row in candidates.leaving_soon.items]) == (
        1, ["Marked Movie"])

    # And the statement is not even asked for on the tab that hides the list.
    _, statement_rows = traced_page(client, conn, "/")
    assert not [row for row in statement_rows if "rr.state='cancelled'" in row]
    _, statement_rows = traced_page(client, conn, "/?tab=leaving_soon")
    assert [row for row in statement_rows if "rr.state='cancelled'" in row]


def test_a_seasons_row_still_counts_only_the_files_of_its_seasons(app, conn):
    """S1 keeps the exact numbers: size, disks and file count of the scope."""
    engine = engine_for(app, conn)
    seed_show(conn, engine, mark_unit=Unit.SEASONS, wanted=(2, 3))
    [row] = attached(conn, app).leaving_soon.items
    assert row.title.startswith("Show")
    assert (row.size_bytes, row.disk, row.file_count) == (4 * GB, "disk2, disk3", 4)


def test_a_series_row_covers_every_present_episode_of_the_series(app, conn):
    engine = engine_for(app, conn)
    series, _ = seed_show(conn, engine)
    # One episode Plex proved absent is outside the scope, whatever is stored.
    conn.execute("UPDATE episodes SET plex_present = 0"
                 " WHERE series_rating_key = '90' AND season_number = 1 AND episode_number = 1")
    conn.commit()
    [row] = attached(conn, app).leaving_soon.items
    assert (row.size_bytes, row.disk, row.file_count) == (5 * GB, "disk1, disk2, disk3", 5)
    # A series Plex proved absent is outside the curated scope: its request
    # leaves the screen, exactly as every other absent item does.
    conn.execute("UPDATE media_items SET plex_present = 0 WHERE id = ?", (series,))
    conn.commit()
    assert attached(conn, app).leaving_soon.items == ()


def test_a_movie_row_still_reports_its_own_file(app, conn):
    engine = engine_for(app, conn)
    library = seed_library(conn, "1", "Movies")
    movie = seed_item(conn, library, "80", "A movie")
    conn.execute("UPDATE media_items SET disk = 'disk3', file_size_bytes = ? WHERE id = ?",
                 (5 * GB, movie))
    conn.commit()
    mark(engine, movie, "80", "A movie")
    [row] = attached(conn, app).leaving_soon.items
    assert (row.title, row.size_bytes, row.disk, row.file_count) == (
        "A movie", 5 * GB, "disk3", 1)


def test_a_damaged_request_stays_unreadable_beside_the_healthy_ones(app, conn):
    """S16 rule 1 survives the batching: one damaged carrier is not fatal."""
    engine = engine_for(app, conn)
    library = seed_library(conn, "1", "Movies")
    healthy = seed_item(conn, library, "60", "Healthy")
    mark(engine, healthy, "60", "Healthy")
    damaged = seed_item(conn, library, "61", "Damaged")
    request = mark(engine, damaged, "61", "Damaged")
    conn.execute("UPDATE candidates SET facts = 'not json' WHERE id = ?",
                 (request.candidate_id,))
    conn.commit()
    items = attached(conn, app).leaving_soon.items
    assert [(row.title, row.unreadable) for row in items] == [
        ("Healthy", False), ("Damaged", True)]
