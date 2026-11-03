"""Finished scans retain the exact displayed facts, including dry previews."""
from datetime import datetime, timezone
import json

import pytest

from almali_library_curator.config import Settings, save_settings
from almali_library_curator.db import init_db
from almali_library_curator.scan import ScanRunner, last_scan
from fake_plex_server import FakePlexDataset, FakePlexServer, movie


@pytest.mark.parametrize('dry_run', [False, True])
def test_finished_scan_persists_candidate_and_protected_display_facts(tmp_path, monkeypatch, dry_run):
    monkeypatch.setenv('ALC_PLEX_TOKEN', 'test-token')
    data = FakePlexDataset()
    data.items['1'] = [movie('11', 'Tenet', rating=7.0), movie('12', 'Severance', rating=8.7)]
    with FakePlexServer(data) as server:
        settings = Settings(config_dir=tmp_path, dry_run=dry_run, scheduler_enabled=False,
                            plex_base_url=server.base_url, plex_rate_limit_per_second=1000)
        save_settings(settings)
        conn = init_db(settings.database_path)
        report = ScanRunner(settings, clock=lambda: datetime(2026, 11, 20, tzinfo=timezone.utc),
                            enrichment_step=lambda *args, **kwargs: {}).run('manual')
        assert report.status == 'finished'
        stored = json.loads(last_scan(conn)['report'])['detection']['counts']
        item = stored['candidate_rows'][0]
        assert item['title'] == 'Tenet'
        assert item['media_item_id'] > 0
        assert item['unit'] == 'movie' and item['seasons'] == []
        assert item['type_badge'] == 'MOVIE'
        assert item['categories'] == ['never_watched']
        assert item['reason'] and item['size_bytes'] == 24 * 1024**3
        assert 'disk' in item and item['rating'] == 7.0 and item['rating_source']
        protected = stored['protected_rows'][0]
        assert protected['title'] == 'Severance'
        assert protected['protection_kind'] == 'rating' and protected['protection_reason']
        assert 'test-token' not in json.dumps(stored)
        if dry_run:
            assert conn.execute('SELECT count(*) FROM media_items').fetchone()[0] == 0
        conn.close()
