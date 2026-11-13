"""Public read models, not presentation markup, define the W9a contract."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from almali_library_curator.config import save_settings
from almali_library_curator.scan import ScanRunner
from lifecycle_helpers import seed_library, seed_item, make_candidate
from almali_library_curator.lifecycle import LifecycleEngine

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def displayed(identifier, title, size, categories=('never_watched',)):
    return dict(media_item_id=identifier, title=title, unit='movie', seasons=[],
                type_badge='MOVIE', categories=list(categories), reasons=['A reason.'],
                reason='A reason.', size_bytes=size, disk='disk3', rating=7.0, rating_source='IMDb')


def snapshot(app, candidates, protected=(), dry_run=True):
    settings = replace(app.config['SETTINGS'], dry_run=dry_run)
    save_settings(settings)
    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=len(candidates), candidate_rows=candidates,
                                          protected_rows=list(protected), protected=len(protected))}
    return ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')


def event(conn, outcome, month, freed, title='Tenet'):
    conn.execute('INSERT INTO removal_events (outcome, title, unit, size_bytes, freed_bytes,'
                 ' disk, reason, marked_by, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (outcome, title, 'movie', 100, freed, 'disk3', 'A reason', 'owner', month+'-01T12:00:00+00:00'))
    conn.commit()


def test_library_and_shell_never_read_the_full_report(app, conn, client):
    snapshot(app, [displayed(1, 'Visible without report', 10)])
    def labels_only(action, table, column, database, source):
        import sqlite3
        if action == sqlite3.SQLITE_READ and table == 'scan_runs' and column == 'report':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(labels_only)
    try:
        from almali_library_curator.scan import last_scan
        from almali_library_curator.web.views import candidates_view, scan_status_view
        assert 'report' in last_scan(conn)
        assert candidates_view(conn).items[0].title == 'Visible without report'
        assert scan_status_view(conn).state == 'finished'
        # Use this same guarded connection for the page and shell requests.
        from unittest.mock import patch
        with patch('almali_library_curator.web.routes.get_db', return_value=conn):
            for url in ('/', '/settings', '/history'):
                assert client.get(url).status_code == 200
    finally:
        conn.set_authorizer(None)


@pytest.mark.parametrize('report', [json.dumps({'sync': {'status': 'finished'}}), 'broken'])
def test_library_views_render_empty_sections_without_detection_counts(app, conn, client, report):
    """A run without detection counts shows empty sections, never a crash."""
    from unittest.mock import patch

    from almali_library_curator.web.views import (candidates_view, library_view, protected_view,
                                                   report_section_rows)
    conn.execute("INSERT INTO scan_runs (started_at, finished_at, status, mode, trigger, report)"
                 " VALUES ('2026-11-20T00:00:00+00:00', '2026-11-20T00:01:00+00:00',"
                 " 'finished', 'dry_run', 'manual', ?)", (report,))
    conn.commit()
    model = candidates_view(conn)
    assert model.items == () and model.category_counts['all'] == 0
    assert protected_view(conn).items == ()
    page = library_view(conn, app.config['SETTINGS'])
    assert page.candidates.items == () and page.protected.items == ()
    # A missing or null section is an empty list, never None.
    assert report_section_rows(conn, 'candidate_rows') == []
    assert report_section_rows(conn, 'protected_rows') == []
    with patch('almali_library_curator.web.routes.get_db', return_value=conn):
        assert client.get('/').status_code == 200


def test_every_detection_count_section_survives_the_display_projection(app, conn):
    """The projection keeps every section of the run's detection counts."""
    from almali_library_curator.web.views import candidates_view, report_section_rows
    settings = replace(app.config['SETTINGS'], dry_run=True)
    save_settings(settings)
    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: dict(candidates=1, protected=0,
                                          candidate_rows=[displayed(1, 'Tenet', 10)],
                                          protected_rows=[],
                                          leaving_rows=[{'title': 'Kept by hand'}])}
    ScanRunner(settings, steps_factory=factory, clock=lambda: NOW).run('manual')
    assert report_section_rows(conn, 'leaving_rows') == [{'title': 'Kept by hand'}]
    assert [row.title for row in candidates_view(conn).items] == ['Tenet']


def test_candidates_read_latest_finished_snapshot_largest_first_with_category_counts(app, conn):
    from almali_library_curator.web.views import candidates_view
    snapshot(app, [displayed(1, 'Old', 999)])
    snapshot(app, [displayed(2, 'Small', 5), displayed(3, 'Large', 100,
                                ('never_watched', 'movies_not_rewatched'))])
    model = candidates_view(conn)
    assert [row.title for row in model.items] == ['Large', 'Small']
    assert model.category_counts['all'] == 2
    assert model.category_counts['never_watched'] == 2
    assert model.category_counts['movies_not_rewatched'] == 1
    assert model.category_counts['stalled_series'] == 0
    assert [row.title for row in candidates_view(conn, 'movies_not_rewatched').items] == ['Large']


def test_dry_snapshot_is_labelled_would_and_protection_keeps_original_reason(app, conn):
    from almali_library_curator.web.views import candidates_view, protected_view
    protected = dict(displayed(2, 'Kept', 20), protection_kind='keep_forever', protection_reason='Kept by hand')
    snapshot(app, [displayed(1, 'Tenet', 10)], [protected])
    assert candidates_view(conn).would is True
    model = protected_view(conn)
    assert model.would is True and model.items[0].protection_reason == 'Kept by hand'
    assert model.items[0].categories == ('never_watched',)


def test_summary_counts_only_verified_deleted_events_and_reports_sources(app, conn):
    from almali_library_curator.web.views import summary_view
    protected = dict(displayed(2, 'Kept', 20), protection_kind='rating', protection_reason='High rating')
    run = snapshot(app, [displayed(1, 'Tenet', 10)], [protected])
    event(conn, 'deleted', '2026-09', 70)
    event(conn, 'cancelled', '2026-10', 999)
    event(conn, 'failed', '2026-11', 999)
    event(conn, 'deleted', '2026-11', 0)
    model = summary_view(conn, app.config['SETTINGS'], clock=lambda: NOW)
    assert (model.candidates.count, model.candidates.size_bytes) == (1, 10)
    assert (model.protected.count, model.protected.size_bytes) == (1, 20)
    assert model.protection_sources == ('rating',)
    assert (model.freed_bytes, model.deleted_count, model.first_month) == (70, 2, '2026-09')
    assert model.last_scan['id'] == run.id and model.grace_days == 14


def marked(conn, app):
    library = seed_library(conn, '1', 'Movies')
    item = seed_item(conn, library, '11', 'Tenet')
    conn.execute('UPDATE media_items SET disk=? WHERE id=?', ('disk3', item))
    conn.commit()
    engine = LifecycleEngine(conn, settings=app.config['SETTINGS'], clock=lambda: NOW)
    request = engine.mark_for_removal(make_candidate(item, '11', 'Tenet'))
    return engine, request


def test_leaving_soon_days_state_scope_and_recent_cancellation_reason(app, conn):
    from almali_library_curator.web.views import leaving_soon_view, summary_view
    engine, request = marked(conn, app)
    model = leaving_soon_view(conn, app.config['SETTINGS'], clock=lambda: NOW)
    row = model.items[0]
    assert row.id == request.id and row.title == 'Tenet' and row.days_left == 14
    assert row.disk == 'disk3' and row.state == 'marked' and row.size_bytes > 0
    assert summary_view(conn, app.config['SETTINGS'], clock=lambda: NOW).leaving_soon.count == 1
    engine.keep_it(request)
    model = leaving_soon_view(conn, app.config['SETTINGS'], clock=lambda: NOW)
    assert model.items == ()
    assert model.cancelled[0].cancel_reason


def test_removal_failed_exposes_error_without_becoming_deleted(app, conn):
    from almali_library_curator.web.views import leaving_soon_view
    _, request = marked(conn, app)
    conn.execute('UPDATE removal_requests SET state=?, last_error=?, recheck_status=? WHERE id=?',
                 ('removal_failed', 'Removal failed: RadarrError', 'passed', request.id))
    conn.commit()
    row = leaving_soon_view(conn, app.config['SETTINGS'], clock=lambda: NOW).items[0]
    assert row.state == 'removal_failed' and row.error == 'Removal failed: RadarrError'
    assert row.recheck_status == 'passed'


def test_damaged_request_is_unreadable_and_does_not_hide_healthy_requests(app, conn):
    from almali_library_curator.web.views import leaving_soon_view
    engine, request = marked(conn, app)
    other = seed_item(conn, 1, '12', 'Other')
    engine.mark_for_removal(make_candidate(other, '12', 'Other'))
    conn.execute('UPDATE candidates SET facts=? WHERE id=?', ('broken-json', request.candidate_id))
    conn.commit()
    rows = leaving_soon_view(conn, app.config['SETTINGS'], clock=lambda: NOW).items
    assert len(rows) == 2
    damaged = next(row for row in rows if row.id == request.id)
    assert damaged.unreadable is True and damaged.state == 'unreadable'
    assert any(row.title == 'Other' and not row.unreadable for row in rows)


def test_history_newest_first_filters_and_monthly_freed(app, conn):
    from almali_library_curator.web.views import history_view
    event(conn, 'deleted', '2026-09', 70)
    event(conn, 'deleted', '2026-11', 30)
    event(conn, 'failed', '2026-11', 999)
    model = history_view(conn)
    assert [row.outcome for row in model.events] == ['failed', 'deleted', 'deleted']
    assert model.freed_per_month == {'2026-11': 30, '2026-09': 70}
    assert len(history_view(conn, outcome='deleted', month='2026-11').events) == 1
    assert history_view(conn, outcome="' OR 1=1 --").events == ()


def test_history_orders_actual_instants_with_different_offsets(app, conn):
    from almali_library_curator.web.views import history_view
    event(conn, 'deleted', '2026-11', 70, title='Earlier')
    event(conn, 'deleted', '2026-11', 30, title='Later')
    conn.execute("UPDATE removal_events SET occurred_at='2026-11-01T12:00:00+01:00' WHERE title='Earlier'")
    conn.execute("UPDATE removal_events SET occurred_at='2026-11-01T11:30:00+00:00' WHERE title='Later'")
    conn.commit()
    assert [row.title for row in history_view(conn).events] == ['Later', 'Earlier']


def test_history_csv_is_one_safe_row_per_filtered_event(client, conn):
    import csv
    import io
    event(conn, 'deleted', '2026-11', 70, title='=HYPERLINK("danger")')
    event(conn, 'failed', '2026-11', 999)
    response = client.get('/history.csv?outcome=deleted&month=2026-11')
    assert response.status_code == 200 and response.mimetype == 'text/csv'
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 1 and rows[0]['freed_bytes'] == '70'
    assert rows[0]['title'].startswith("'=")
    assert not {'file_path', 'plex_token', 'sonarr_api_key'} & rows[0].keys()


@pytest.mark.parametrize('screen', ['/', '/history', '/connections', '/settings'])
def test_screen_routes_provide_typed_view_models(client, app, screen):
    from flask import template_rendered
    rendered = []
    def record(sender, template, context, **kwargs):
        rendered.append(context['model'])
    with template_rendered.connected_to(record, app):
        response = client.get(screen)
    assert response.status_code == 200
    assert rendered and hasattr(rendered[0], '__dataclass_fields__')


def test_library_view_reads_the_scan_report_and_the_leaving_view_once(app, conn):
    """The Library page builds each sub-view once (issue #7)."""
    from almali_library_curator.web.views import library_view
    snapshot(app, [displayed(1, 'Tenet', 10)])
    statements = []
    conn.set_trace_callback(statements.append)
    library_view(conn, app.config['SETTINGS'], clock=lambda: NOW)
    conn.set_trace_callback(None)
    assert sum(1 for statement in statements if 'scan_runs' in statement) == 1
    assert sum(1 for statement in statements if 'removal_requests' in statement) == 2


def test_library_view_still_shows_candidates_protected_and_leaving(app, conn):
    from almali_library_curator.web.views import library_view
    marked(conn, app)
    protected = dict(displayed(2, 'Kept', 20), protection_kind='rating', protection_reason='High rating')
    snapshot(app, [displayed(1, 'Tenet', 10)], [protected])
    model = library_view(conn, app.config['SETTINGS'], clock=lambda: NOW)
    assert [row.title for row in model.candidates.items] == ['Tenet']
    assert [row.title for row in model.protected.items] == ['Kept']
    assert [row.title for row in model.leaving_soon.items] == ['Tenet']
    assert (model.summary.candidates.count, model.summary.candidates.size_bytes) == (1, 10)
    assert (model.summary.protected.count, model.summary.protected.size_bytes) == (1, 20)
    assert model.summary.leaving_soon.count == 1
    assert model.summary.protection_sources == ('rating',)
    assert model.summary.last_scan['id'] == model.candidates.last_scan['id']


def test_settings_view_never_exposes_secret_fields(app, conn):
    from almali_library_curator.web.views import settings_view
    settings = replace(app.config['SETTINGS'], plex_token='never-show', radarr_api_key='never-show')
    model = settings_view(conn, settings)
    assert 'never-show' not in repr(model)
    assert 'plex_token' not in model.values and 'radarr_api_key' not in model.values
