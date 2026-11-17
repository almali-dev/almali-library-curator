"""Validated settings saves are atomic and exclude all credential inputs."""
from dataclasses import replace
from html import unescape
import re

import pytest
from flask import template_rendered

from almali_library_curator.config import save_settings


def test_settings_save_reloads_on_next_request_and_thresholds_reach_scan(client, app):
    from almali_library_curator.web.resources import get_settings
    from almali_library_curator.scan import ScanRunner
    submitted = {
        'grace_days': '30', 'minimum_rating': '9.1', 'scan_time': '07:45',
        'collection_name': 'Farewell', 'dry_run': 'false', 'rating_protection_enabled': 'false',
        'protected_libraries': 'Movies, Shows', 'excluded_users': 'Guest',
        'never_watched_months': '9',
    }
    # #64: turning dry run off asks first; the panel's own button saves it all.
    asking = client.post('/settings', data=submitted)
    assert asking.status_code == 200
    confirm_action = re.search(r'<form class="settings-form" method="post" action="([^"]+)"',
                               asking.get_data(as_text=True)).group(1)
    response = client.post(unescape(confirm_action), data=submitted)
    assert response.status_code == 303
    with client.session_transaction() as session:
        assert session['_flashes'][-1][1] == 'web.flash.dry_run_off'
    with app.test_request_context('/'):
        loaded = get_settings()
        assert loaded.grace_days == 30 and loaded.minimum_rating == 9.1
        assert loaded.scan_time == '07:45' and loaded.collection_name == 'Farewell'
        assert loaded.dry_run is False and loaded.rating_protection_enabled is False
        assert loaded.protected_libraries == ('Movies', 'Shows') and loaded.excluded_users == ('Guest',)
        assert loaded.thresholds.never_watched_months == 9
    seen = []
    def factory(conn, settings, *args):
        seen.append(settings.thresholds.never_watched_months)
        return {name: lambda: {} for name in ('sync', 'enrichment', 'detection', 'recheck', 'deletion')}
    report = ScanRunner(app.config['SETTINGS'], steps_factory=factory).run('manual')
    assert report.status == 'finished' and seen == [9]


@pytest.mark.parametrize('field,value,key', [
    ('grace_days', '2', 'grace_range'), ('grace_days', '61', 'grace_range'),
    ('grace_days', 'abc', 'grace_range'),
    ('minimum_rating', '-1', 'rating_range'), ('minimum_rating', '11', 'rating_range'),
    ('minimum_rating', 'nan', 'rating_range'), ('minimum_rating', 'inf', 'rating_range'),
    ('scan_time', '25:00', 'scan_time'), ('scan_time', '6:00', 'scan_time'),
    ('collection_name', '   ', 'collection_required'),
    ('never_watched_months', '0', 'threshold_range'), ('stalled_series_months', '-1', 'threshold_range'),
    ('everyone_finished_months', '1.5', 'threshold_range'),
    ('dry_run', 'nonsense', 'boolean'), ('rating_protection_enabled', 'nonsense', 'boolean'),
    ('plex_token', 'never-save', 'secrets_unsupported'), ('radarr_api_key', 'never-save', 'secrets_unsupported'),
])
def test_invalid_settings_change_nothing_and_rerender_with_error(client, app, field, value, key):
    path = app.config['SETTINGS'].config_dir / 'settings.json'
    before = path.read_bytes()
    captured = []
    def record(sender, template, context, **kwargs):
        captured.append(context['model'])
    with template_rendered.connected_to(record, app):
        response = client.post('/settings', data={'grace_days': '30', field: value})
    assert response.status_code == 400
    assert path.read_bytes() == before
    assert captured[-1].errors[field] == 'web.form.' + key
    assert 'never-save' not in repr(captured[-1])


@pytest.mark.parametrize('grace', ['3', '60'])
def test_grace_boundary_values_are_accepted(client, grace):
    assert client.post('/settings', data={'grace_days': grace}).status_code == 303
