"""S3 and S4 (#148): the Library request reads its scope and its sources once.

The Library page asks for the curated scope from five readers (the totals,
the page count, the page rows, the category counts and the protection
sources) and the shell asks for the sources panel beside the page itself.
S3 resolves both once per request; S4 makes the poster fallback lookup an
index search instead of a table scan. Every assertion reads the statements
the real code emitted, through ``EXPLAIN QUERY PLAN`` where a plan is the
subject.
"""

from __future__ import annotations

import json

import pytest

THUMB = "/library/metadata/11/thumb/1700000000"


def publish(conn, candidates=(), protected=()):
    """Publish one finished snapshot, exactly as the scan's own publish does."""
    report = {"detection": {"counts": {
        "candidates": len(candidates), "protected": len(protected),
        "candidate_rows": list(candidates), "protected_rows": list(protected)}}}
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger, report)"
        " VALUES ('2026-11-27T10:00:00+00:00', '2026-11-27T10:01:00+00:00',"
        " 'finished', 'dry_run', 'manual', ?)", (json.dumps(report),))
    conn.commit()


def traced(conn, action):
    """Everything ``action`` runs, and its result."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, statements


def plans(conn, statements):
    """The query plan of every read one call emitted."""
    details = []
    for statement in statements:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            details.extend(row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN " + statement, [None] * statement.count("?")).fetchall())
    return details


def test_the_poster_fallback_lookup_reads_an_index(app, conn):
    """S4: a dry run's thumb is found by index, never by scanning the rows."""
    from almali_library_curator.web.posters import stored_thumb

    publish(conn, [dict(media_item_id=1, title="Tenet", unit="movie", thumb=THUMB)])
    # No live item row: the thumb exists only in the published snapshot,
    # which is exactly the dry-run case the lookup exists for.
    thumb, statements = traced(conn, lambda: stored_thumb(conn, 1))
    assert thumb == THUMB
    details = plans(conn, statements)
    assert [detail for detail in details if "scan_rows" in detail]
    assert not [detail for detail in details
                if "scan_rows" in detail and detail.startswith("SCAN")], details
    assert any("idx_scan_rows_item" in detail for detail in details), details


def test_the_poster_fallback_still_answers_the_newest_row_and_none(conn):
    """S4 keeps S3's answer: the newest thumb of the item, or no thumb."""
    from almali_library_curator.web.posters import stored_thumb

    publish(conn, [dict(media_item_id=1, title="Tenet", unit="movie", thumb=THUMB)])
    publish(conn, [dict(media_item_id=1, title="Tenet", unit="movie", thumb=None),
                   dict(media_item_id=2, title="Heat", unit="movie", thumb=None)])
    assert stored_thumb(conn, 1) is None  # the newest row carries no thumb
    assert stored_thumb(conn, 2) is None
    assert stored_thumb(conn, 999) is None
    publish(conn, [dict(media_item_id=2, title="Heat", unit="movie",
                        thumb="/library/metadata/12/thumb/1700000100")])
    assert stored_thumb(conn, 2) == "/library/metadata/12/thumb/1700000100"


def test_the_library_request_reads_the_scope_once(app, conn, client):
    """S3: one resolution serves the totals, the counts and the page."""
    from unittest.mock import patch

    from almali_library_curator.scan import rows as scan_rows

    conn.execute("INSERT INTO libraries (plex_key, title, section_type)"
                 " VALUES ('1', 'Movies', 'movie')")
    conn.execute(
        "INSERT INTO media_items (library_id, plex_rating_key, title, item_type, plex_present)"
        " VALUES (1, '11', 'Tenet', 'movie', 1), (1, '31', 'Gone', 'movie', 0)")
    # One proven-absent row is what makes the scope restrict anything, so the
    # page has to resolve the probe before it reads any row.
    publish(conn, [dict(media_item_id=1, title="Tenet", unit="movie", size_bytes=10),
                   dict(media_item_id=3, title="Gone", unit="movie", size_bytes=20)])
    conn.commit()

    statements = []
    with patch("almali_library_curator.scan.rows._scope_template",
               wraps=scan_rows._scope_template) as resolutions, \
            patch("almali_library_curator.web.routes.get_db", return_value=conn):
        conn.set_trace_callback(statements.append)
        try:
            page = client.get("/").get_data(as_text=True)
        finally:
            conn.set_trace_callback(None)

    # One resolution for the whole page: the totals, the count, the page of
    # rows, the category counts and the protection sources all reuse it.
    assert resolutions.call_count == 1
    # The probe that decides whether the scope restricts anything is read
    # twice: once by the page facts the Leaving soon decode uses, once by the
    # resolved scope of the rows.
    probes = [statement for statement in statements
              if "FROM media_items WHERE plex_present = 0" in statement]
    assert len(probes) == 2, probes
    # And the scope still filters: the proven-absent item is not on the page.
    assert "Tenet" in page and "Gone" not in page


def test_the_shell_builds_the_sources_panel_once_per_page(app, conn, client):
    """S3: ``sources_view`` used to run twice: the page and the shell."""
    from unittest.mock import patch

    from almali_library_curator.web import views

    calls = []
    original = views._sources_view

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    with patch("almali_library_curator.web.views._sources_view", side_effect=counted), \
            patch("almali_library_curator.web.routes.get_db", return_value=conn):
        for screen in ("/", "/?tab=protected", "/?tab=leaving_soon", "/history", "/settings"):
            calls.clear()
            assert client.get(screen).status_code == 200
            assert len(calls) == 1, (screen, len(calls))


def test_the_sources_panel_is_read_once_per_request_and_never_cached_across(app, conn):
    """S3: the one read belongs to the request, not to the process."""
    from flask import g

    from almali_library_curator.web.views import sources_view

    with app.test_request_context("/"):
        first = sources_view(conn, app.config["SETTINGS"])
        assert first == sources_view(conn, app.config["SETTINGS"])
        assert "sources_view" in g
    # Outside a request the panel is never cached: another worker's write to
    # the same store is visible on the next read.
    conn.execute("INSERT INTO users (plex_user_id, title) VALUES ('9', 'New')")
    conn.execute("INSERT INTO play_events (user_id, plex_rating_key, watched, played_at)"
                 " VALUES ((SELECT id FROM users WHERE plex_user_id = '9'), '11', 1,"
                 " '2021-01-01T00:00:00+00:00')")
    conn.commit()
    assert sources_view(conn, app.config["SETTINGS"]).plex_users == first.plex_users + 1
