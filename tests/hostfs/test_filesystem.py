"""Host-filesystem adapter tests (S2 Unraid host filesystem, S11 psutil).

Every test builds a real directory tree under ``tmp_path`` that mirrors an
Unraid layout — array disks (``disk1``, ``disk2``), a cache pool and the
merged ``user`` share view — and talks to the adapter only through its public
interface. Hardlinks are real ``os.link`` links. No fake callables are
injected: the adapter uses the real ``os.stat`` and ``psutil.disk_usage``.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from almali_library_curator.config import Settings
from almali_library_curator.hostfs import HostFilesystem

ARRIVAL_REL = Path("Media/Movies/Arrival (2016)/Arrival (2016).mkv")
PILOT_REL = Path("Media/Series/Lost in Space/Season 02/pilot.mkv")
DUNE_REL = Path("Media/Movies/Dune (2021)/Dune (2021).mkv")

# psutil rounds ``percent`` and the bracket bounds below are three separately
# computed floats; a thousandth of a percentage point absorbs that rounding
# without hiding a wrong reading.
PERCENT_EPSILON = 1e-3


@pytest.fixture()
def unraid_tree(tmp_path: Path) -> dict[str, Path]:
    """A miniature Unraid ``/mnt`` plus the container's ``/media`` view."""
    unraid = tmp_path / "unraid"
    media = tmp_path / "media"

    copies = {
        unraid / "disk1" / ARRIVAL_REL: b"a" * 100,
        unraid / "disk2" / PILOT_REL: b"b" * 200,
        unraid / "cache" / DUNE_REL: b"c" * 300,
        # The merged user-share view exposes every file, whatever disk holds it.
        unraid / "user" / ARRIVAL_REL: b"a" * 100,
        unraid / "user" / PILOT_REL: b"b" * 200,
        unraid / "user" / DUNE_REL: b"c" * 300,
        # The app's own read-only view of the media share.
        media / ARRIVAL_REL: b"a" * 100,
        media / PILOT_REL: b"b" * 200,
        media / DUNE_REL: b"c" * 300,
    }
    for path, payload in copies.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {"unraid": unraid, "media": media}


@pytest.fixture()
def host_fs(unraid_tree: dict[str, Path]) -> HostFilesystem:
    return HostFilesystem(
        unraid_root=unraid_tree["unraid"],
        media_root=unraid_tree["media"],
    )


class TestDiskFor:
    def test_finds_the_array_disk_holding_the_file(self, host_fs, unraid_tree):
        media = unraid_tree["media"]
        assert host_fs.disk_for(media / PILOT_REL) == "disk2"

    def test_finds_a_cache_pool_file(self, host_fs, unraid_tree):
        media = unraid_tree["media"]
        assert host_fs.disk_for(media / DUNE_REL) == "cache"

    def test_never_reports_the_merged_user_view_as_the_disk(
        self, host_fs, unraid_tree
    ):
        # The file also exists under ``user``, but a pool name must be returned.
        media = unraid_tree["media"]
        assert host_fs.disk_for(media / ARRIVAL_REL) == "disk1"

    def test_returns_none_when_file_exists_only_in_the_user_view(
        self, host_fs, unraid_tree
    ):
        media, unraid = unraid_tree["media"], unraid_tree["unraid"]
        ghost_media = media / "Media/Movies/ghost/ghost.mkv"
        ghost_media.parent.mkdir(parents=True, exist_ok=True)
        ghost_media.write_bytes(b"g")
        ghost_user = unraid / "user" / "Media/Movies/ghost/ghost.mkv"
        ghost_user.parent.mkdir(parents=True, exist_ok=True)
        ghost_user.write_bytes(b"g")
        assert host_fs.disk_for(ghost_media) is None

    def test_degraded_mode_when_unraid_root_is_empty(self, unraid_tree):
        fs = HostFilesystem(unraid_root="", media_root=unraid_tree["media"])
        assert fs.disk_for(unraid_tree["media"] / PILOT_REL) is None

    def test_degraded_mode_when_unraid_root_does_not_exist(self, tmp_path, unraid_tree):
        fs = HostFilesystem(
            unraid_root=tmp_path / "no-such-mnt",
            media_root=unraid_tree["media"],
        )
        assert fs.disk_for(unraid_tree["media"] / PILOT_REL) is None

    def test_returns_none_for_missing_file(self, host_fs, unraid_tree):
        missing = unraid_tree["media"] / "Media/Movies/absent/absent.mkv"
        assert host_fs.disk_for(missing) is None

    def test_returns_none_for_path_outside_the_media_root(
        self, host_fs, unraid_tree
    ):
        outside = unraid_tree["unraid"] / "disk2" / PILOT_REL
        assert host_fs.disk_for(outside) is None

    def test_accepts_a_path_already_relative_to_the_media_root(
        self, host_fs, unraid_tree
    ):
        assert host_fs.disk_for(DUNE_REL) == "cache"


class TestHardlinkCount:
    def test_counts_two_real_links(self, host_fs, unraid_tree):
        original = unraid_tree["media"] / "Media/Movies/twin/a.mkv"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"x" * 128)
        os.link(original, original.with_name("a (copy).mkv"))
        assert host_fs.hardlink_count(original) == 2

    def test_counts_three_real_links(self, host_fs, unraid_tree):
        original = unraid_tree["media"] / "Media/Movies/triplet/a.mkv"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"x" * 128)
        os.link(original, original.with_name("b.mkv"))
        os.link(original, original.with_name("c.mkv"))
        assert host_fs.hardlink_count(original) == 3

    def test_single_link_file_counts_one(self, host_fs, unraid_tree):
        original = unraid_tree["media"] / ARRIVAL_REL
        assert host_fs.hardlink_count(original) == 1

    def test_missing_file_counts_zero(self, host_fs, unraid_tree):
        missing = unraid_tree["media"] / "Media/Movies/absent/absent.mkv"
        assert host_fs.hardlink_count(missing) == 0


class TestFileSize:
    def test_returns_the_size_of_a_present_file(self, host_fs, unraid_tree):
        assert host_fs.file_size(unraid_tree["media"] / PILOT_REL) == 200

    def test_returns_zero_for_an_empty_file(self, host_fs, unraid_tree):
        empty = unraid_tree["media"] / "Media/Movies/empty/empty.mkv"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        assert host_fs.file_size(empty) == 0

    def test_returns_none_for_a_missing_file(self, host_fs, unraid_tree):
        missing = unraid_tree["media"] / "Media/Movies/absent/absent.mkv"
        assert host_fs.file_size(missing) is None


class TestDiskUsage:
    def test_reports_consistent_usage_math_for_a_real_root(
        self, host_fs, unraid_tree
    ):
        import psutil

        root = os.fspath(unraid_tree["unraid"] / "disk2")
        # The root is a live filesystem: any write between the calls below
        # (another test, another process on the machine) moves used, free and
        # percent without the adapter being wrong. So the adapter's reading
        # is bracketed between two raw readings of the same disk instead of
        # being compared for equality with one of them.
        before = psutil.disk_usage(root)
        usage = host_fs.disk_usage("disk2")
        after = psutil.disk_usage(root)
        assert usage is not None
        # ``total`` does not move with writes, so it stays an equality: an
        # adapter reading a different filesystem would still fail here.
        assert usage.total == before.total == after.total
        assert min(before.used, after.used) <= usage.used <= max(
            before.used, after.used
        )
        assert min(before.free, after.free) <= usage.free <= max(
            before.free, after.free
        )
        assert (
            min(before.percent, after.percent) - PERCENT_EPSILON
            <= usage.percent
            <= max(before.percent, after.percent) + PERCENT_EPSILON
        )
        # And the numbers are self-consistent usage math.
        assert usage.total > 0
        assert 0.0 <= usage.percent <= 100.0
        assert 0 <= usage.used <= usage.total
        assert 0 <= usage.free <= usage.total
        assert usage.used + usage.free <= usage.total

    def test_reports_the_filesystem_reading_even_when_usage_moves_between_reads(
        self, host_fs, unraid_tree, monkeypatch
    ):
        import psutil

        root = os.fspath(unraid_tree["unraid"] / "disk2")
        before = psutil.disk_usage(root)
        usage = host_fs.disk_usage("disk2")
        assert usage is not None
        # A write lands after the adapter read: the next raw reading of the
        # same live disk reports ``delta`` more used and ``delta`` less free,
        # and its percent rises by at least ``delta * 100 / total`` points
        # under psutil's usage math. Only the raw reading this test takes is
        # moved -- the adapter keeps the real psutil -- and nothing is
        # written to the measured root.
        delta = 8 * 1024 * 1024
        moved = types.SimpleNamespace(
            total=before.total,
            used=before.used + delta,
            free=before.free - delta,
            percent=before.percent + 100.0 * delta / before.total,
        )
        assert moved.used - before.used == delta
        monkeypatch.setattr(psutil, "disk_usage", lambda path: moved)
        after = psutil.disk_usage(root)
        assert usage.total == before.total == after.total
        assert min(before.used, after.used) <= usage.used <= max(
            before.used, after.used
        )
        assert min(before.free, after.free) <= usage.free <= max(
            before.free, after.free
        )
        assert (
            min(before.percent, after.percent) - PERCENT_EPSILON
            <= usage.percent
            <= max(before.percent, after.percent) + PERCENT_EPSILON
        )

    def test_returns_none_for_an_absent_root(self, host_fs):
        assert host_fs.disk_usage("disk99") is None

    def test_returns_none_in_degraded_mode(self, unraid_tree):
        fs = HostFilesystem(unraid_root="", media_root=unraid_tree["media"])
        assert fs.disk_usage("disk2") is None


class TestIsMounted:
    def test_true_for_a_readable_media_root(self, host_fs, unraid_tree):
        assert host_fs.is_mounted(unraid_tree["media"]) is True

    def test_defaults_to_the_media_root(self, host_fs, unraid_tree):
        assert host_fs.is_mounted() is True

    def test_false_for_a_missing_path(self, host_fs, tmp_path):
        assert host_fs.is_mounted(tmp_path / "not-mounted") is False


class TestFromSettings:
    def test_builds_a_working_adapter_from_settings_attributes(
        self, unraid_tree
    ):
        settings = type(
            "SettingsLike",
            (),
            {
                "unraid_root": str(unraid_tree["unraid"]),
                "media_root": str(unraid_tree["media"]),
            },
        )()
        fs = HostFilesystem.from_settings(settings)
        assert fs.disk_for(unraid_tree["media"] / PILOT_REL) == "disk2"

    def test_degrades_when_settings_leave_the_paths_unconfigured(self, tmp_path):
        settings = Settings(
            config_dir=tmp_path / "config",
            unraid_root="",
            media_root=str(tmp_path / "absent-media"),
        )
        fs = HostFilesystem.from_settings(settings)
        assert fs.disk_for(tmp_path / "absent-media/Movies/x.mkv") is None
        assert fs.is_mounted() is False

    def test_settings_attributes_reach_the_adapter(self, tmp_path, monkeypatch, unraid_tree):
        monkeypatch.setenv("ALC_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("ALC_UNRAID_ROOT", str(unraid_tree["unraid"]))
        monkeypatch.setenv("ALC_MEDIA_ROOT", str(unraid_tree["media"]))

        from almali_library_curator.config import load_settings

        fs = HostFilesystem.from_settings(load_settings())

        assert fs.is_mounted() is True
        assert fs.disk_for(unraid_tree["media"] / PILOT_REL) == "disk2"
