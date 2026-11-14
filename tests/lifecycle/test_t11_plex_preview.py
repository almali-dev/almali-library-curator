"""T11: read-only Plex previews agree with recorded deletion refusals."""

from copy import deepcopy
from dataclasses import replace

import pytest

from almali_library_curator.detection.models import Unit
from test_s15_plex_fallback import Scenario, WRITES
from test_s3_plex_multi_version import multi_version_item


def assert_refusal_parity(scenario, request, code):
    """Preview and execute the same fixture without changing its media."""
    expected = [("failed", f"removal.failed.{code}")]
    before_db = tuple(scenario.conn.iterdump())
    before_files = {path: path.read_bytes() for path in scenario.root.glob("*.mkv")}
    before_collections = deepcopy(vars(scenario.engine.collections))
    scenario.engine.settings = replace(scenario.settings, dry_run=True)
    with scenario.servers() as (executor, plex, radarr, sonarr):
        preview = executor.run_deletions(passed_ids=scenario.passed_ids)
        requests = [r for server in (plex, radarr, sonarr) for r in server.requests]
    assert not [r for r in requests if r["method"] in WRITES]
    assert tuple(scenario.conn.iterdump()) == before_db
    assert {path: path.read_bytes() for path in before_files} == before_files
    assert vars(scenario.engine.collections) == before_collections

    scenario.engine.settings = scenario.settings
    real = scenario.run()
    assert [(o.status, o.error) for o in real.outcomes] == expected
    assert [(o.status, o.error) for o in preview.outcomes] == expected
    assert any(r["path"].startswith("/library/metadata/") for r in requests)
    assert scenario.plex_deletes == []
    assert scenario.arr_writes == []
    assert {path: path.read_bytes() for path in before_files} == before_files
    row = scenario.row(request)
    assert (row["state"], row["last_error"], row["deleted_at"]) == (
        "removal_failed", expected[0][1], None)
    events = scenario.conn.execute(
        "SELECT outcome, error, freed_bytes FROM removal_events").fetchall()
    assert [tuple(row) for row in events] == [("failed", expected[0][1], 0)]


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SEASONS, Unit.SERIES])
def test_multi_version_preview_matches_real_refusal_without_mutations(conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    other = tmp_path / "other-version.mkv"
    other.write_bytes(b"other version")
    key = "item-1" if unit is Unit.MOVIE else "item-1-s1e1"
    scenario.plex.metadata[key] = multi_version_item(
        key, "Title 1", "/data/movie.mkv", "/data/other-version.mkv")
    assert_refusal_parity(scenario, request, "plex_multi_version")


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SEASONS, Unit.SERIES])
def test_missing_plex_item_preview_matches_real_files_present_without_mutations(
        conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit)
    request = scenario.add()
    # missing_media affects DELETE only; remove metadata to model an item
    # Plex no longer knows on the read-only preflight as well.
    scenario.plex.metadata.clear()
    assert_refusal_parity(scenario, request, "files_present")


@pytest.mark.parametrize("status,code", [
    (403, "plex_refused"), (401, "plex_auth"), (503, "plex_unavailable"),
])
def test_read_failures_keep_the_real_plex_diagnostic(conn, tmp_path, status, code):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    if status == 401:
        scenario.plex.required_token = "rejected-token"
    else:
        scenario.plex.fail_always("/library/metadata/item-1", status)
    assert_refusal_parity(scenario, request, code)


@pytest.mark.parametrize("unit", [Unit.MOVIE, Unit.SEASONS, Unit.SERIES])
def test_known_single_version_items_still_preview_then_delete(conn, tmp_path, unit):
    scenario = Scenario(conn, tmp_path, unit=unit)
    scenario.add()
    before_db = tuple(conn.iterdump())
    scenario.engine.settings = replace(scenario.settings, dry_run=True)
    with scenario.servers() as (executor, plex, radarr, sonarr):
        preview = executor.run_deletions(passed_ids=scenario.passed_ids)
        assert all(r["method"] == "GET" for server in (plex, radarr, sonarr)
                   for r in server.requests)
    assert [(o.status, o.error) for o in preview.outcomes] == [("would_delete", None)]
    assert tuple(conn.iterdump()) == before_db
    assert all(path.exists() for path in scenario.files.values())
    scenario.engine.settings = scenario.settings
    assert [o.status for o in scenario.run().outcomes] == ["deleted"]
    if unit is Unit.SEASONS:
        assert scenario.files["item-1-s2e1"].exists()
    else:
        assert not any(path.exists() for path in scenario.files.values())
