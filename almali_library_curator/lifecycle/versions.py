"""Which version of an item the app suggests keeping (#133).

The owner decided the rule: "the version to propose keeping is the one with
the highest resolution". Ties go to the file the download manager manages (so
removing the other version stays possible, because the app refuses to
remove that managed file), then the higher bitrate, then the larger file, then the version Plex
listed first. The suggestion only preselects; the owner confirms.
"""

from __future__ import annotations

from typing import Any, Sequence

# Plex ``videoResolution`` values, lowest first. Plex reports a UHD file as
# "4k"; "2160" is the same resolution written as lines, so both rank alike.
_ORDER = ("sd", "480", "576", "720", "1080", "1440", "4k", "8k")
_ALIASES = {"2160": "4k", "2160p": "4k", "4320": "8k", "1080p": "1080", "720p": "720"}


def resolution_rank(resolution: str | None) -> int:
    """A sortable rank: higher is sharper, 0 for an unknown resolution."""
    value = (resolution or "").strip().lower()
    value = _ALIASES.get(value, value)
    return _ORDER.index(value) + 1 if value in _ORDER else 0


def suggest_kept_version(versions: Sequence[Any], managed_file: str | None = None):
    """The version to suggest keeping, or ``None`` when no rule can choose.

    ``versions`` are rows or objects with ``position``, ``resolution``,
    ``file_path``, ``bitrate_kbps`` and ``size_bytes``. A version without a
    file is never suggested; when no candidate has a known resolution
    nothing is suggested and the owner picks.
    """
    def field(version, name):
        try:
            return version[name]
        except (TypeError, KeyError, IndexError):
            return getattr(version, name, None)

    candidates = [v for v in versions
                  if field(v, "file_path") and resolution_rank(field(v, "resolution")) > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (
        resolution_rank(field(v, "resolution")),
        managed_file is not None and field(v, "file_path") == managed_file,
        field(v, "bitrate_kbps") or 0,
        field(v, "size_bytes") or 0,
        -(field(v, "position") or 0),
    ))
