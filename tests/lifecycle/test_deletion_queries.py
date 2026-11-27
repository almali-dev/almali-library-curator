"""S5 (#148): a seasons removal reads Sonarr's episode files once per run.

The executor asked Sonarr for the episode files of a series twice: once to
prove read-only that the arr owns exactly the marked files, and again to pick
the ids it deletes. One read answers both, so a seasons removal costs one
request instead of two, and the files it deletes are exactly the same ones.
"""

from __future__ import annotations

from test_deletion import Scenario

from almali_library_curator.detection.models import Unit

EPISODE_FILES = "/api/v3/episodefile"
BULK_DELETE = "/api/v3/episodefile/bulk"


def test_a_seasons_removal_reads_the_episode_files_once(conn, tmp_path):
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add()
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [outcome.status for outcome in report.outcomes] == ["deleted"]
    assert len(sonarr.requests_for(EPISODE_FILES)) == 1
    # The exact same outcome: only the file of the marked season is deleted.
    assert sonarr.requests_for(BULK_DELETE)[-1]["body"]["episodeFileIds"] == [11]
    assert not scenario.paths[1][0].exists()
    assert scenario.paths[1][1].exists()


def test_a_seasons_preview_reads_the_episode_files_once(conn, tmp_path):
    """The dry run proves the same ownership, and pays for it once."""
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add()
    with scenario.dry_executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [outcome.status for outcome in report.outcomes] == ["would_delete"]
    assert len(sonarr.requests_for(EPISODE_FILES)) == 1
    assert not sonarr.requests_for(BULK_DELETE)


def test_a_series_removal_is_unaffected_by_the_cache(conn, tmp_path):
    """A series removal never reads the episode files, and still deletes."""
    scenario = Scenario(conn, tmp_path, unit=Unit.SERIES)
    scenario.add()
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [outcome.status for outcome in report.outcomes] == ["deleted"]
    assert not sonarr.requests_for(EPISODE_FILES)


def test_two_seasons_removals_each_read_their_own_episode_files(conn, tmp_path):
    """One read per removal: a second removal is never answered from the first."""
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add(1)
    scenario.add(2)
    with scenario.executor() as (executor, _, sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
    assert [outcome.status for outcome in report.outcomes] == ["deleted", "deleted"]
    assert len(sonarr.requests_for(EPISODE_FILES)) == 2
    assert [request["body"]["episodeFileIds"]
            for request in sonarr.requests_for(BULK_DELETE)] == [[11], [21]]
