"""#133 T4: which version the app suggests keeping.

S2 (owner, verbatim): "the version to propose keeping is the one with the
**highest resolution**." Ties go to the file Radarr manages, then the higher
bitrate, then the larger size, then the first position. A version without a
file is never suggested, and nothing is suggested when no resolution is known.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from almali_library_curator.lifecycle.versions import resolution_rank, suggest_kept_version


@dataclass
class V:
    position: int
    resolution: str | None
    file_path: str | None = "/m/f.mkv"
    bitrate_kbps: int | None = None
    size_bytes: int | None = None


def test_the_highest_resolution_is_suggested():
    versions = [V(0, "1080", "/a"), V(1, "4k", "/b"), V(2, "720", "/c")]
    assert suggest_kept_version(versions).position == 1


@pytest.mark.parametrize("higher,lower", [
    ("8k", "4k"), ("4k", "1440"), ("1440", "1080"), ("1080", "720"),
    ("720", "576"), ("576", "480"), ("480", "sd"), ("sd", None), ("SD", "unknown-thing"),
])
def test_the_resolution_order(higher, lower):
    assert resolution_rank(higher) > resolution_rank(lower)


def test_4k_and_2160_are_the_same_resolution():
    assert resolution_rank("4k") == resolution_rank("2160") == resolution_rank("4K")


def test_a_tie_goes_to_the_file_radarr_manages_first():
    versions = [V(0, "1080", "/a", bitrate_kbps=9000), V(1, "1080", "/b", bitrate_kbps=1)]
    assert suggest_kept_version(versions, managed_file="/b").position == 1


def test_then_to_the_higher_bitrate():
    versions = [V(0, "1080", "/a", bitrate_kbps=4000, size_bytes=99),
                V(1, "1080", "/b", bitrate_kbps=8000, size_bytes=1)]
    assert suggest_kept_version(versions).position == 1


def test_then_to_the_larger_file():
    versions = [V(0, "1080", "/a", bitrate_kbps=None, size_bytes=5),
                V(1, "1080", "/b", bitrate_kbps=None, size_bytes=9)]
    assert suggest_kept_version(versions).position == 1


def test_then_to_the_first_position():
    versions = [V(3, "1080", "/a"), V(1, "1080", "/b")]
    assert suggest_kept_version(versions).position == 1


def test_a_version_without_a_file_is_never_suggested():
    versions = [V(0, "4k", None), V(1, "1080", "/b")]
    assert suggest_kept_version(versions).position == 1


def test_nothing_is_suggested_when_no_resolution_is_known():
    assert suggest_kept_version([V(0, None, "/a"), V(1, "", "/b")]) is None
    assert suggest_kept_version([]) is None
