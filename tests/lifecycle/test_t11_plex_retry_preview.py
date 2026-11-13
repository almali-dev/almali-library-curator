"""Already-gone Plex retry evidence remains valid in the read-only preview."""

from dataclasses import replace

import pytest

from almali_library_curator.detection.models import Unit
from test_s15_plex_fallback import Scenario


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SEASONS])
def test_missing_item_already_gone_on_retry_does_not_block_known_siblings(
        conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    missing = "item-1" if unit is Unit.MOVIE else "item-1-s1e1"
    del scenario.plex.metadata[missing]
    first = scenario.run()
    assert [o.error for o in first.outcomes] == ["removal.failed.files_present"]
    # The real attempt saw every scope file. Its evidence allows a retry
    # to accept this file's disappearance, even though Plex knows nothing.
    scenario.files[missing].unlink()
    if unit is Unit.SEASONS:
        # A known sibling still needs deletion; its presence must NOT be
        # confused with the missing item's file during the preview.
        scenario.files["item-1-s1e2"].write_bytes(b"known sibling")
    scenario.clock.advance(days=1)
    scenario.passed_ids = [o.request.id for o in scenario.engine.run_recheck().passed]
    assert scenario.passed_ids == [request.id]
    before_db = tuple(conn.iterdump())
    before_files = {key: path.read_bytes() for key, path in scenario.files.items()
                    if path.exists()}
    scenario.engine.settings = replace(scenario.settings, dry_run=True)
    with scenario.servers() as (executor, plex, radarr, sonarr):
        preview = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert all(r["method"] == "GET" for server in (plex, radarr, sonarr)
                   for r in server.requests)
    assert [(o.status, o.error) for o in preview.outcomes] == [("would_delete", None)]
    assert tuple(conn.iterdump()) == before_db
    assert {key: scenario.files[key].read_bytes() for key in before_files} == before_files
    scenario.engine.settings = scenario.settings
    assert [o.status for o in scenario.run().outcomes] == ["deleted"]
    assert scenario.row(request)["state"] == "deleted"
    assert not scenario.files[missing].exists()
    if unit is Unit.SEASONS:
        assert scenario.plex_deletes == ["/library/metadata/item-1-s1e2"]
        assert scenario.files["item-1-s2e1"].exists()
    else:
        assert scenario.plex_deletes == []
