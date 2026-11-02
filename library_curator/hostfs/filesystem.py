"""Unraid host-filesystem adapter (S2 optional source, S11 psutil).

The app sees media through the container's ``/media`` mount, which on Unraid
is usually a merged user share: one path that hides which array disk or cache
pool physically holds each file. When the optional ``/unraid/mnt`` mount is
present, this adapter maps a media path back to the physical disk by testing
the same relative path under every disk root beneath ``unraid_root``.

Without that mount the app degrades gracefully: sizes are shown without a
disk and freed space is marked as estimated (S2). No method here ever raises
for a missing path.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import psutil

logger = logging.getLogger(__name__)

StatFn = Callable[[str | os.PathLike[str]], os.stat_result]


class UsageLike(Protocol):
    """The shape of ``psutil.disk_usage``'s result (a private namedtuple)."""

    total: int
    used: int
    free: int
    percent: float


UsageFn = Callable[[str | os.PathLike[str]], UsageLike]


def _optional_path(value: str | os.PathLike[str] | None) -> Path | None:
    """``Path(value)`` or ``None`` when empty (``Path("")`` would be the CWD)."""
    if value is None:
        return None
    text = os.fspath(value)
    return Path(text) if text else None

# Directories under Unraid's /mnt that are not disk roots: the merged user
# share views, Unraid's own addon mount, root shares and user-mounted disks.
NON_DISK_ROOTS = frozenset({"user", "user0", "addons", "rootshare", "disks"})


@dataclass(frozen=True)
class DiskUsage:
    """Usage of one array disk or pool, for the summary cards (S7)."""

    total: int
    used: int
    free: int
    percent: float


def _root_sort_key(name: str) -> tuple[int, int, str]:
    """Array disks first in numeric order, then pools alphabetically."""
    match = re.fullmatch(r"disk(\d+)", name)
    if match:
        return (0, int(match.group(1)), "")
    return (1, 0, name)


class HostFilesystem:
    """Answers "which disk holds this file?" for a merged Unraid media share.

    ``unraid_root`` is the host path that maps to Unraid's ``/mnt`` (the
    container's ``/unraid/mnt``); ``media_root`` is the container's ``/media``
    mount as the app sees it. Both default to empty: the adapter then runs in
    degraded mode and every disk question answers ``None``/``False``.
    """

    def __init__(
        self,
        unraid_root: str | os.PathLike[str] = "",
        media_root: str | os.PathLike[str] = "",
        *,
        stat: StatFn = os.stat,
        disk_usage: UsageFn = psutil.disk_usage,
    ) -> None:
        self._unraid_root = _optional_path(unraid_root)
        self._media_root = _optional_path(media_root)
        self._stat = stat
        self._disk_usage = disk_usage
        self._disk_root_names: list[str] | None = None
        self._degraded_warned = False

    def __repr__(self) -> str:
        unraid = str(self._unraid_root) if self._unraid_root is not None else ""
        media = str(self._media_root) if self._media_root is not None else ""
        return f"HostFilesystem(unraid_root={unraid!r}, media_root={media!r})"

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> "HostFilesystem":
        """Build an adapter from the application settings.

        Tolerates settings that do not carry ``unraid_root``/``media_root``
        yet: the adapter simply stays in degraded mode until they exist.
        """
        options: dict[str, Any] = {
            "unraid_root": getattr(settings, "unraid_root", ""),
            "media_root": getattr(settings, "media_root", ""),
        }
        options.update(overrides)
        return cls(**options)

    # -- Disk attribution -------------------------------------------------

    def disk_for(self, path: str | os.PathLike[str]) -> str | None:
        """Name of the array disk or pool that holds ``path``, or ``None``.

        The same relative path is tested (with ``os.stat``) under each disk
        root beneath ``unraid_root``; the first root whose copy exists wins.
        ``None`` means degraded mode (no ``unraid_root``), a path outside the
        media root, or a file no disk claims. A missing file never raises.
        """
        roots = self._disk_roots()
        if not roots:
            return None
        relative = self._relative_under_media(Path(path))
        if relative is None:
            return None
        for name in roots:
            try:
                self._stat(self._unraid_root / name / relative)
            except OSError:
                continue
            return name
        return None

    def disk_usage(self, root_name: str) -> DiskUsage | None:
        """Usage of the disk root ``root_name`` via ``psutil``, or ``None``.

        ``None`` when the adapter is degraded or the root is absent.
        """
        if self._unraid_root is None:
            return None
        if not root_name or root_name in {".", ".."} or "/" in root_name or "\\" in root_name:
            return None
        root = self._unraid_root / root_name
        try:
            usage = self._disk_usage(os.fspath(root))
        except OSError:
            return None
        return DiskUsage(
            total=int(usage.total),
            used=int(usage.used),
            free=int(usage.free),
            percent=float(usage.percent),
        )

    # -- File facts -------------------------------------------------------

    def hardlink_count(self, path: str | os.PathLike[str]) -> int:
        """``st_nlink`` of ``path``; 0 when missing, 1 when unavailable."""
        try:
            return int(self._stat(path).st_nlink)
        except FileNotFoundError:
            return 0
        except OSError:
            return 1

    def file_size(self, path: str | os.PathLike[str]) -> int | None:
        """Size of ``path`` in bytes, or ``None`` when it is absent."""
        try:
            return int(self._stat(path).st_size)
        except OSError:
            return None

    def is_mounted(self, path: str | os.PathLike[str] | None = None) -> bool:
        """Whether ``path`` (default: the media root) is a readable directory."""
        target = self._media_root if path is None else Path(path)
        if target is None:
            return False
        try:
            return target.is_dir() and os.access(target, os.R_OK)
        except OSError:
            return False

    # -- Internals ----------------------------------------------------------

    def _disk_roots(self) -> list[str]:
        """Disk-root names under ``unraid_root``, cached for the process."""
        if self._disk_root_names is None:
            self._disk_root_names = self._scan_disk_roots()
        return self._disk_root_names

    def _scan_disk_roots(self) -> list[str]:
        if self._unraid_root is None:
            return []
        try:
            entries = os.listdir(self._unraid_root)
        except OSError:
            if not self._degraded_warned:
                self._degraded_warned = True
                logger.warning(
                    "Unraid host filesystem is not mounted; "
                    "sizes will be shown without disk attribution",
                    extra={"unraid_root": str(self._unraid_root)},
                )
            return []
        names = [
            entry
            for entry in entries
            if entry not in NON_DISK_ROOTS and (self._unraid_root / entry).is_dir()
        ]
        return sorted(names, key=_root_sort_key)

    def _relative_under_media(self, path: Path) -> Path | None:
        """Path relative to the media root, or ``None`` when unmappable.

        A relative ``path`` is taken as already relative to the media root.
        """
        if not path.is_absolute():
            return path
        if self._media_root is None:
            return None
        try:
            return path.relative_to(self._media_root)
        except ValueError:
            return None
