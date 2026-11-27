"""S3 (#139): only a stored Plex thumb path can become a poster request."""

import pytest

from almali_library_curator.plex.posters import is_poster_thumb


@pytest.mark.parametrize("thumb", [
    "/library/metadata/11/thumb/1700000000",
    "/library/metadata/123456/thumb/1",
])
def test_a_plex_thumb_path_is_a_poster(thumb):
    assert is_poster_thumb(thumb)


@pytest.mark.parametrize("thumb", [
    None, "", "https://evil.example/x.jpg", "//evil.example/x.jpg",
    "/library/metadata/11/thumb/1700000000?X-Plex-Token=t",
    "/library/metadata/11/thumb/1700000000/../../../:/prefs",
    "/library/metadata/11/art/1700000000", "/library/metadata/x/thumb/1",
    "/library/metadata/11/thumb/1700000000\n", "library/metadata/11/thumb/1",
    "/photo/:/transcode?url=/library/metadata/11/thumb/1", 11,
])
def test_anything_else_is_not_a_poster(thumb):
    assert not is_poster_thumb(thumb)
