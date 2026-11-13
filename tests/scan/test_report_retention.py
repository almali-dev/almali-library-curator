"""Bounded reports without losing the last successful Library snapshot."""
import json

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db
from almali_library_curator.scan import ScanRunner, last_scan
from almali_library_curator.web.views import candidates_view, scan_status_view


def test_reports_are_bounded_and_space_is_reclaimed_on_an_existing_database(tmp_path):
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    # Model an existing installation with large historical reports.
    for _ in range(30):
        conn.execute("INSERT INTO scan_runs (status, report) VALUES ('failed', ?)",
                     (json.dumps({'padding': 'x' * 100_000}),))
    conn.commit()
    before = conn.execute('PRAGMA page_count').fetchone()[0]
    factory = lambda *args: {name: lambda: {} for name in
                            ('sync', 'enrichment', 'detection', 'recheck', 'deletion')}
    runner = ScanRunner(settings, steps_factory=factory)
    for _ in range(30):
        assert runner.run('manual').status == 'finished'
    assert conn.execute('SELECT COUNT(*) FROM scan_runs WHERE report IS NOT NULL').fetchone()[0] == 30
    assert conn.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0] == 60
    assert conn.execute('PRAGMA page_count').fetchone()[0] < before / 2
    assert 'report' in last_scan(conn)
    conn.close()


def test_last_scan_report_holds_the_bounded_step_summary(tmp_path):
    """The report key is the step summary, never the pruned full report."""
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    factory = lambda *args: {name: lambda: {} for name in
                             ('sync', 'enrichment', 'detection', 'recheck', 'deletion')}
    assert ScanRunner(settings, steps_factory=factory).run('manual').status == 'finished'
    labels = last_scan(conn)
    assert 'report' in labels
    body = json.loads(labels['report'])
    assert body == {'sync': 'finished', 'enrichment': 'finished', 'detection': 'finished',
                    'recheck': 'finished', 'deletion': 'finished'}
    # The bounded summary carries no rows: the full report stays pruned.
    assert 'candidate_rows' not in body and 'protected_rows' not in body
    assert len(labels['report']) < 500
    conn.close()


def test_failed_attempts_do_not_erase_the_latest_successful_display(tmp_path):
    settings = Settings(config_dir=tmp_path, dry_run=False, scheduler_enabled=False)
    save_settings(settings)
    conn = init_db(settings.database_path)
    row = dict(media_item_id=1, title='Retained', unit='movie', seasons=[],
               type_badge='MOVIE', categories=['never_watched'], reasons=['Old'],
               reason='Old', size_bytes=10, disk=None, rating=None, rating_source=None)
    def factory(*args):
        return {**{name: lambda: {} for name in ('sync', 'enrichment', 'recheck', 'deletion')},
                'detection': lambda: {'candidate_rows': [row]}}
    runner = ScanRunner(settings, steps_factory=factory)
    first = runner.run('manual')
    def broken(*args):
        raise RuntimeError('no snapshot')
    runner.steps_factory = broken
    for _ in range(31):
        assert runner.run('manual').status == 'failed'
    assert conn.execute('SELECT report FROM scan_runs WHERE id=?', (first.id,)).fetchone()[0] is None
    assert [item.title for item in candidates_view(conn).items] == ['Retained']
    assert last_scan(conn)['id'] == first.id
    assert scan_status_view(conn).state == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM scan_run_views').fetchone()[0] <= 31
    conn.close()
