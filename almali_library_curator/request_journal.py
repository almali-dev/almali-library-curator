"""A bounded journal of the outbound requests an HTTP client made (#101)."""

from __future__ import annotations

from typing import Any

DEFAULT_JOURNAL_SIZE = 256


class RequestJournal(list):
    """A list that keeps only its ``maxlen`` most recent entries.

    The arr clients journal every outbound attempt so a caller can see
    the exact wire traffic beside what a server saw. The clients are
    long-lived (the scan builds them once per run), so the journal is
    bounded: the oldest entries are dropped as new ones arrive. It stays
    a plain list for every reader.
    """

    def __init__(self, maxlen: int = DEFAULT_JOURNAL_SIZE) -> None:
        if maxlen < 1:
            raise ValueError("a request journal keeps at least one entry")
        super().__init__()
        self.maxlen = maxlen

    def append(self, entry: Any) -> None:
        super().append(entry)
        overflow = len(self) - self.maxlen
        if overflow > 0:
            del self[:overflow]
