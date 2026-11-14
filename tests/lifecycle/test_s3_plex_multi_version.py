"""S3: the Plex fallback refuses a multi-version item before any deletion.

The first real-server verification (2026-11-13) found 199 items with more
than one ``Media`` entry, every version with its own distinct file, and no
``selected``/``primary``/``isPrimary``/``default`` marker anywhere in the
payload. The app stores one path and one size per item, so the marked scope
covers one file while ``DELETE /library/metadata/<ratingKey>`` removes every
version. The deletion path therefore re-reads the item and refuses, sending
nothing, whenever more than one version has a distinct file.
"""

from __future__ import annotations

from fake_plex_server import movie
from test_s15_plex_fallback import Scenario

from almali_library_curator.detection.models import Unit


def version_media(*files: str) -> list[dict]:
    """The live multi-version shape: one ``Media`` entry per distinct file.

    Each entry carries its own ``Part`` array, exactly as the live server
    answers, and no marker names the primary version.
    """
    return [
        {
            "id": index,
            "videoProfile": "high",
            "Part": [
                {
                    "container": "MKV",
                    "duration": 2_700_000,
                    "file": file,
                    "id": 1000 + index,
                    "indexes": "sd",
                    "key": f"/library/parts/{index}/file.mkv",
                    "size": 24 * 1024**3,
                    "videoProfile": "high",
                }
            ],
        }
        for index, file in enumerate(files, start=1)
    ]


def multi_version_item(rating_key: str, title: str, *files: str) -> dict:
    return movie(rating_key, title, Media=version_media(*files))


def test_a_multi_version_item_is_refused_and_nothing_is_sent(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.plex.metadata["item-1"] = multi_version_item(
        "item-1", "Title 1",
        "/data/movie-4k.mkv", "/data/movie-1080p.mkv")
    report = scenario.run()
    key = "removal.failed.plex_multi_version"
    assert [(o.status, o.error) for o in report.outcomes] == [("failed", key)]
    # The refusal sends nothing: no DELETE reached Plex, and every file the
    # versions point at is untouched on disk.
    assert scenario.plex_deletes == []
    assert scenario.files["item-1"].exists()
    row = scenario.row(request)
    assert (row["state"], row["last_error"]) == ("removal_failed", key)
    # The refusal is a decision the owner can act on: it retries, like the
    # other removal failures.
    scenario.clock.advance(days=1)
    assert [o.request.id for o in scenario.engine.run_recheck().passed] == [request.id]


def test_the_refusal_does_not_depend_on_the_document_order(conn, tmp_path):
    # Three versions with distinct files, with the marked file neither
    # first nor second: the count is what matters, never which entry the
    # server happened to list first.
    scenario = Scenario(conn, tmp_path)
    request = scenario.add()
    scenario.plex.metadata["item-1"] = multi_version_item(
        "item-1", "Title 1",
        "/data/movie-other.mkv", "/data/movie.mkv", "/data/movie-4k.mkv")
    report = scenario.run()
    assert [(o.status, o.error) for o in report.outcomes] == [
        ("failed", "removal.failed.plex_multi_version")]
    assert scenario.plex_deletes == []
    assert scenario.files["item-1"].exists()
    assert scenario.row(request)["state"] == "removal_failed"


def test_a_single_version_item_keeps_today_s_deletion(conn, tmp_path):
    scenario = Scenario(conn, tmp_path)
    scenario.add()
    report = scenario.run()
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert scenario.plex_deletes == ["/library/metadata/item-1"]
    assert not scenario.files["item-1"].exists()


def test_the_reread_adds_no_request_for_items_not_deleted_through_plex(conn, tmp_path):
    # A dry run sends no mutations (read-only Plex preflight is allowed),
    # and a deletion owned by Radarr never talks to Plex: metadata checks
    # apply only to the Plex route, including its preview.
    dry_root = tmp_path / "dry"
    dry_root.mkdir()
    from almali_library_curator.db import init_db
    dry_conn = init_db(dry_root / "lifecycle.db")
    try:
        dry = Scenario(dry_conn, dry_root, dry_run=True)
        dry.add()
        dry.passed_ids = [r["id"] for r in dry_conn.execute(
            "SELECT id FROM removal_requests")]
        dry.run()
        assert dry.plex_deletes == []
    finally:
        dry_conn.close()

    scenario = Scenario(conn, tmp_path, radarr=True)
    scenario.add()
    with scenario.servers() as (executor, plex, _radarr, _sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        plex_requests = list(plex.requests)
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert plex_requests == []


def test_the_plex_path_rereads_the_item_once_before_the_delete(conn, tmp_path):
    # The re-read is one GET per deleted item, no more: it must not double
    # the traffic or hit items outside the deletion.
    scenario = Scenario(conn, tmp_path, unit=Unit.SEASONS)
    scenario.add(seasons=(1,))
    with scenario.servers() as (executor, plex, _radarr, _sonarr):
        report = executor.run_deletions(passed_ids=scenario.passed_ids)
        reads = [r["path"] for r in plex.requests
                 if r["method"] == "GET" and r["path"] == "/library/metadata/item-1-s1e1"]
        methods = [(r["method"], r["path"]) for r in plex.requests]
    assert [o.status for o in report.outcomes] == ["deleted"]
    assert reads == ["/library/metadata/item-1-s1e1"]
    assert ("DELETE", "/library/metadata/item-1-s1e1") in methods
    assert ("GET", "/library/metadata/item-1-s1e2") in methods
    # The untouched sibling episode of the other season is never read.
    assert ("/library/metadata/item-1-s2e1",) not in [
        (path,) for _method, path in methods]
