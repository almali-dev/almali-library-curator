"""S2 (#132): every version of an item, as Plex already answers it.

A live server sends one ``Media`` entry per version of the item, and each
entry carries the facts of that version: ``videoResolution``,
``videoCodec``, ``bitrate`` (kbps) and its own ``Part`` files. Until this
task only the first part's path and size and the ``Media`` ``id`` and
``videoProfile`` were read, so the other versions were invisible.
"""

from __future__ import annotations

from test_plex_media_parts import live_movie, live_part

from almali_library_curator.plex.models import PlexMetadata, PlexVersion


def live_media(media_id: int, resolution: str, codec: str, bitrate: int,
               parts: list[dict], **extra) -> dict:
    """One ``Media`` entry with the keys a live server really sends."""
    data = {
        "id": media_id,
        "duration": 7_200_000,
        "bitrate": bitrate,
        "width": 3840,
        "height": 2160,
        "aspectRatio": "1.78",
        "videoResolution": resolution,
        "videoCodec": codec,
        "container": "mkv",
        "videoFrameRate": "24p",
        "videoProfile": "main",
        "Part": parts,
    }
    data.update(extra)
    return data


def two_version_movie() -> dict:
    return live_movie("11", "Heat", [
        live_media(31, "4k", "hevc", 25_000,
                   [live_part("/media/Movies/Heat/Heat - 2160p.mkv", 40 * 1024**3)]),
        live_media(32, "1080", "h264", 8_000,
                   [live_part("/media/Movies/Heat/Heat - 1080p.mkv", 12 * 1024**3)]),
    ])


def test_every_version_carries_its_own_resolution_codec_bitrate_and_size():
    item = PlexMetadata(**two_version_movie())

    assert item.versions == (
        PlexVersion(media_id=31, video_profile="main", resolution="4k",
                    video_codec="hevc", bitrate_kbps=25_000,
                    file_path="/media/Movies/Heat/Heat - 2160p.mkv",
                    size_bytes=40 * 1024**3),
        PlexVersion(media_id=32, video_profile="main", resolution="1080",
                    video_codec="h264", bitrate_kbps=8_000,
                    file_path="/media/Movies/Heat/Heat - 1080p.mkv",
                    size_bytes=12 * 1024**3),
    )


def test_a_missing_field_is_unknown_never_zero():
    entry = live_media(31, "1080", "h264", 8_000, [live_part("/media/Heat.mkv", 1024)])
    for key in ("videoResolution", "videoCodec", "bitrate"):
        del entry[key]

    [version] = PlexMetadata(**live_movie("11", "Heat", [entry])).versions

    assert (version.resolution, version.video_codec, version.bitrate_kbps) == (
        None, None, None)


def test_a_stacked_version_counts_every_file_it_holds():
    """One version can be several parts (a multi-disc film): its size is
    the space the whole version takes, not only its first file."""
    payload = live_movie("11", "Heat", [
        live_media(31, "1080", "h264", 8_000, [
            live_part("/media/Movies/Heat/Heat - disc1.mkv", 8 * 1024**3),
            live_part("/media/Movies/Heat/Heat - disc2.mkv", 7 * 1024**3),
        ]),
    ])

    [version] = PlexMetadata(**payload).versions

    assert version.file_path == "/media/Movies/Heat/Heat - disc1.mkv"
    assert version.size_bytes == 15 * 1024**3


def test_a_version_without_a_size_is_unknown_and_never_zero():
    payload = live_movie("11", "Heat", [
        live_media(31, "1080", "h264", 8_000,
                   [{"file": "/media/Movies/Heat/Heat.mkv"}]),
    ])

    [version] = PlexMetadata(**payload).versions

    assert version.size_bytes is None


def test_an_item_without_media_has_no_versions():
    """The older payload shape keeps working: it carries a top-level part,
    and it says nothing about versions."""
    payload = {
        "ratingKey": "11",
        "type": "movie",
        "title": "Heat",
        "Part": [live_part("/media/Movies/Heat/Heat.mkv", 24 * 1024**3)],
    }

    item = PlexMetadata(**payload)

    assert item.versions == ()
    assert item.file_path == "/media/Movies/Heat/Heat.mkv"
    assert item.file_size == 24 * 1024**3


def test_the_primary_file_is_still_the_first_version_in_document_order():
    """The existing behaviour (#37) is untouched by reading every version."""
    item = PlexMetadata(**two_version_movie())

    assert item.file_path == "/media/Movies/Heat/Heat - 2160p.mkv"
    assert item.file_size == 40 * 1024**3
