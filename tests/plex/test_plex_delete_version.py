"""#133 T2: removing one version of a Plex item, and nothing else.

``DELETE /library/metadata/{ratingKey}/media/{mediaId}`` is the request
Plex Web (and python-plexapi ``Media.delete``) sends for one version. The
item is read again first: the version must still be the stored file, a
single part, and another version with its own file must remain. Any doubt
refuses and sends nothing. Removing a whole item keeps refusing several
versions (S5).
"""

from __future__ import annotations

import pytest

from almali_library_curator.plex import PlexClient
from almali_library_curator.plex.deletion import (
    PlexDeletionRefused,
    PlexMultiVersionError,
    PlexVersionChanged,
    PlexVersionParts,
    delete_media_item,
    delete_media_version,
    preflight_media_version,
)
from fake_plex_server import FakePlexDataset, FakePlexServer, movie

KEY = "11"
FOUR_K = "/media/Movies/Tenet/Tenet 2160p.mkv"
HD = "/media/Movies/Tenet/Tenet 1080p.mkv"


def part(path, part_id):
    return {"id": part_id, "file": path, "size": 10, "key": f"/library/parts/{part_id}/f.mkv"}


def two_versions(**overrides):
    media = [
        {"id": 501, "videoResolution": "4k", "Part": [part(FOUR_K, 1)]},
        {"id": 502, "videoResolution": "1080", "Part": [part(HD, 2)]},
    ]
    media.extend(overrides.pop("extra", []))
    return movie(KEY, "Tenet", Media=overrides.pop("Media", media))


def client_for(server):
    return PlexClient(server.base_url, "test-token", rate_limit_per_second=0,
                      max_retries=0, backoff_seconds=0)


def run(dataset, action):
    with FakePlexServer(dataset) as server:
        result = action(client_for(server))
        deletes = [r["path"] for r in server.requests if r["method"] == "DELETE"]
    return result, deletes


def dataset_with(item):
    dataset = FakePlexDataset()
    dataset.metadata[KEY] = item
    return dataset


def test_one_version_is_deleted_with_its_own_media_path():
    dataset = dataset_with(two_versions())

    result, deletes = run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))

    assert result is True
    assert deletes == [f"/library/metadata/{KEY}/media/502"]
    assert dataset.deleted_versions == [(KEY, 502)]
    # The kept version is still on the item; the whole item was never deleted.
    assert [m["id"] for m in dataset.metadata[KEY]["Media"]] == [501]
    assert dataset.deleted_media == []


def test_the_preflight_reads_only():
    dataset = dataset_with(two_versions())

    result, deletes = run(dataset, lambda c: preflight_media_version(c, KEY, 502, HD))

    assert result is True
    assert deletes == []


def test_a_path_that_no_longer_matches_is_refused_and_nothing_is_sent():
    dataset = dataset_with(two_versions())

    with pytest.raises(PlexVersionChanged):
        run(dataset, lambda c: delete_media_version(c, KEY, 502, FOUR_K))
    assert dataset.deleted_versions == []


def test_a_media_id_plex_no_longer_has_is_refused():
    dataset = dataset_with(two_versions())

    with pytest.raises(PlexVersionChanged):
        run(dataset, lambda c: delete_media_version(c, KEY, 999, HD))
    assert dataset.deleted_versions == []


def test_the_last_version_is_never_removed():
    only = movie(KEY, "Tenet", Media=[{"id": 502, "Part": [part(HD, 2)]}])
    dataset = dataset_with(only)

    with pytest.raises(PlexVersionChanged):
        run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))
    assert dataset.deleted_versions == []


def test_another_version_holding_the_same_file_does_not_count_as_kept():
    same = [
        {"id": 501, "Part": [part(HD, 1)]},
        {"id": 502, "Part": [part(HD, 2)]},
    ]
    dataset = dataset_with(two_versions(Media=same))

    with pytest.raises(PlexVersionChanged):
        run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))
    assert dataset.deleted_versions == []


def test_a_version_of_several_parts_is_refused():
    stacked = [
        {"id": 501, "Part": [part(FOUR_K, 1)]},
        {"id": 502, "Part": [part(HD, 2), part(HD.replace(".mkv", " cd2.mkv"), 3)]},
    ]
    dataset = dataset_with(two_versions(Media=stacked))

    with pytest.raises(PlexVersionParts):
        run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))
    assert dataset.deleted_versions == []


def test_an_item_plex_no_longer_knows_answers_false_and_sends_nothing():
    dataset = FakePlexDataset()

    result, deletes = run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))

    assert result is False
    assert deletes == []


def test_a_refused_delete_names_the_plex_setting():
    dataset = dataset_with(two_versions())
    dataset.fail_always(f"/library/metadata/{KEY}/media/502", 403)

    with pytest.raises(PlexDeletionRefused):
        run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))
    assert dataset.deleted_versions == []


def test_a_version_gone_between_the_check_and_the_delete_answers_false():
    dataset = dataset_with(two_versions())
    dataset.missing_versions.add((KEY, 502))

    result, deletes = run(dataset, lambda c: delete_media_version(c, KEY, 502, HD))

    assert result is False
    assert deletes == [f"/library/metadata/{KEY}/media/502"]
    assert dataset.deleted_versions == []


def test_removing_the_whole_item_still_refuses_several_versions():
    dataset = dataset_with(two_versions())

    with pytest.raises(PlexMultiVersionError):
        run(dataset, lambda c: delete_media_item(c, KEY))
    assert dataset.deleted_media == [] and dataset.deleted_versions == []
