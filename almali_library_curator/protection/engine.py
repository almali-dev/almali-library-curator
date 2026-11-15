"""The protection engine (S4).

The engine answers one question per item: what protection covers it?
Three protections exist — a protected library, a high rating and a
hand-pressed "Keep forever" — and when several apply, the deterministic
precedence is library, then rating, then kept forever.

The engine only filters and records protections. It never creates,
marks or otherwise advances a removal, so removing a protection can only
make items candidates again.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from almali_library_curator.config import Settings
from almali_library_curator.detection import text
from almali_library_curator.detection.models import Candidate, Unit
from almali_library_curator.i18n import DEFAULT_LOCALE
from almali_library_curator.protection.models import (
    ProtectedCandidate,
    Protection,
    ProtectionKind,
    ProtectionOutcome,
)

logger = logging.getLogger(__name__)


def keep_forever_detail(unit: Unit, seasons: Sequence[int]) -> str:
    """The stored scope of a kept-forever row: its unit and exact seasons.

    A seasons row stores ``seasons:2,3`` (sorted, deduplicated); a series
    or movie row stores its bare unit. The scope is what a kept-forever
    protection applies to: the same media item, the same unit and the
    same set of seasons — never the whole series, never other seasons.
    """
    if unit is Unit.SEASONS:
        numbers = ",".join(str(number) for number in sorted(set(seasons)))
        return f"{unit.value}:{numbers}"
    return unit.value


def parse_keep_forever_detail(detail: str | None) -> tuple[Unit, tuple[int, ...]] | None:
    """Decode a kept-forever scope; ``None`` when the detail is malformed."""
    if not detail:
        return None
    unit_text, separator, seasons_text = detail.partition(":")
    try:
        unit = Unit(unit_text)
    except ValueError:
        return None
    if unit is not Unit.SEASONS:
        return (unit, ()) if not separator else None
    if not seasons_text:
        return None
    try:
        numbers = tuple(sorted({int(part) for part in seasons_text.split(",") if part}))
    except ValueError:
        return None
    return unit, numbers


# The size of the chunks of a query, so a long list of ids never exceeds
# the number of parameters SQLite accepts.
_CHUNK = 500


def _chunks(values: list[int]):
    """The values in chunks of a size every SQLite accepts."""
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


class ProtectionEngine:
    """Decides what protection covers an item (S4)."""

    def __init__(self, conn: sqlite3.Connection, *, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.locale = settings.locale or DEFAULT_LOCALE

    # -- public interface -------------------------------------------------

    def protection_for(self, candidate: Candidate) -> Protection | None:
        """The protection covering ``candidate``, or ``None``.

        The answer follows the database and the settings, so the same
        state always answers the same way: this is the entry point the
        final re-check before deletion reuses (S4, S5 step 4).
        """
        return self._protection_for(candidate, self._protected_libraries())

    def protect(self, candidates: Sequence[Candidate]) -> ProtectionOutcome:
        """Split ``candidates`` into the survivors and the protected."""
        protected_libraries = self._protected_libraries()
        library_ids = self._library_ids(candidates)
        keep_forever = self._keep_forever_protections(candidates)
        survivors: list[Candidate] = []
        protected: list[ProtectedCandidate] = []
        for candidate in candidates:
            protection = self._protection_for(candidate, protected_libraries, library_ids, keep_forever)
            if protection is None:
                survivors.append(candidate)
            else:
                parts = self._reason_parts(protection)
                protected.append(
                    ProtectedCandidate(
                        candidate=candidate,
                        protection=protection,
                        reason=text.render_reason(self.locale, parts),
                        reason_parts=parts,
                    )
                )
        logger.info(
            "protection_applied",
            extra={"candidates": len(survivors), "protected": len(protected)},
        )
        return ProtectionOutcome(candidates=tuple(survivors), protected=tuple(protected))

    def keep_forever(self, candidate: Candidate) -> None:
        """Persist the "Keep forever" protection for exactly this row."""
        detail = keep_forever_detail(candidate.unit, candidate.seasons)
        existing = self.conn.execute(
            "SELECT id FROM protections WHERE kind = ? AND media_item_id = ?"
            " AND detail = ?",
            (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id, detail),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO protections (kind, media_item_id, detail)"
                " VALUES (?, ?, ?)",
                (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id, detail),
            )
        self.conn.commit()
        logger.info(
            "keep_forever_recorded",
            extra={"media_item_id": candidate.media_item_id, "detail": detail},
        )

    def undo_keep_forever(self, candidate: Candidate) -> None:
        """Remove the "Keep forever" protection of exactly this row,
        making the item a candidate again.

        A damaged stored scope belongs to no unit and no set of seasons, so
        it is cleared too: the owner must be able to undo a protection the
        engine could not read.
        """
        detail = keep_forever_detail(candidate.unit, candidate.seasons)
        self.conn.execute(
            "DELETE FROM protections WHERE kind = ? AND media_item_id = ?"
            " AND detail = ?",
            (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id, detail),
        )
        damaged = self.conn.execute(
            "SELECT id, detail FROM protections WHERE kind = ? AND media_item_id = ?",
            (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id),
        ).fetchall()
        for row in damaged:
            if parse_keep_forever_detail(row["detail"]) is None:
                self.conn.execute(
                    "DELETE FROM protections WHERE id = ?", (row["id"],)
                )
        self.conn.commit()
        logger.info(
            "keep_forever_removed",
            extra={"media_item_id": candidate.media_item_id, "detail": detail},
        )

    def protected_library_ids(self) -> tuple[int, ...]:
        """Ids of the libraries protected by their flag or by the settings."""
        return tuple(sorted(self._protected_libraries()))

    # -- resolution ---------------------------------------------------------

    def _protection_for(
        self,
        candidate: Candidate,
        protected_libraries: dict[int, str],
        library_ids: dict[int, int] | None = None,
        keep_forever: dict[int, tuple[str, ...]] | None = None,
    ) -> Protection | None:
        """One item's protection, in the S4 precedence order: library,
        then rating, then kept forever.

        A whole pass hands over the facts it already read, so one query
        answers for every candidate instead of one query per candidate.
        """
        library = self._library_protection(candidate, protected_libraries, library_ids)
        if library is not None:
            return library
        rating = self._rating_protection(candidate)
        if rating is not None:
            return rating
        return self._keep_forever_protection(candidate, keep_forever)

    def _protected_libraries(self) -> dict[int, str]:
        """The protected libraries with their titles.

        A library is protected when its ``libraries.protected`` flag is
        set or when ``settings.protected_libraries`` names it — a
        ``plex_key`` matched exactly, a ``title`` case-insensitively.
        It is unprotected only when neither source names it any more.
        """
        names = {
            name.strip() for name in self.settings.protected_libraries if name.strip()
        }
        titles = {name.casefold() for name in names}
        libraries: dict[int, str] = {}
        for row in self.conn.execute(
            "SELECT id, plex_key, title, protected FROM libraries"
        ):
            if (
                row["protected"]
                or row["plex_key"] in names
                or (row["title"] or "").casefold() in titles
            ):
                libraries[int(row["id"])] = row["title"]
        return libraries

    def _library_ids(self, candidates: Sequence[Candidate]) -> dict[int, int]:
        """The library of the items of this pass, read once for the pass.

        Only the items of the pass are read, so a pass over one item does
        not read the whole library.
        """
        ids = [candidate.media_item_id for candidate in candidates]
        library_ids: dict[int, int] = {}
        for chunk in _chunks(ids):
            marks = ", ".join("?" for _ in chunk)
            for row in self.conn.execute(
                f"SELECT id, library_id FROM media_items WHERE id IN ({marks})", chunk
            ):
                if row["library_id"] is not None:
                    library_ids[int(row["id"])] = int(row["library_id"])
        return library_ids

    def _library_protection(
        self,
        candidate: Candidate,
        protected_libraries: dict[int, str],
        library_ids: dict[int, int] | None = None,
    ) -> Protection | None:
        if library_ids is None:
            row = self.conn.execute(
                "SELECT library_id FROM media_items WHERE id = ?",
                (candidate.media_item_id,),
            ).fetchone()
            if row is None or row["library_id"] is None:
                return None
            library_id = int(row["library_id"])
        else:
            library_id = library_ids.get(candidate.media_item_id)
            if library_id is None:
                return None
        title = protected_libraries.get(library_id)
        if title is None:
            return None
        return Protection(kind=ProtectionKind.LIBRARY, library_id=library_id, detail=title)

    def _rating_protection(self, candidate: Candidate) -> Protection | None:
        if not self.settings.rating_protection_enabled:
            return None
        if candidate.rating is None or candidate.rating < self.settings.minimum_rating:
            return None
        return Protection(
            kind=ProtectionKind.RATING,
            media_item_id=candidate.media_item_id,
            detail=text.format_rating(candidate.rating),
        )

    def _keep_forever_protections(self, candidates: Sequence[Candidate]) -> dict[int, tuple[str, ...]]:
        """The "Keep forever" scopes of the items of this pass, read once."""
        ids = [candidate.media_item_id for candidate in candidates]
        grouped: dict[int, list[str]] = {}
        for chunk in _chunks(ids):
            marks = ", ".join("?" for _ in chunk)
            for row in self.conn.execute(
                "SELECT media_item_id, detail FROM protections"
                f" WHERE kind = ? AND media_item_id IN ({marks}) ORDER BY id",
                (ProtectionKind.KEEP_FOREVER.value, *chunk),
            ):
                if row["media_item_id"] is None:
                    continue
                grouped.setdefault(int(row["media_item_id"]), []).append(row["detail"])
        return {key: tuple(values) for key, values in grouped.items()}

    def _keep_forever_protection(
        self, candidate: Candidate, keep_forever: dict[int, tuple[str, ...]] | None = None
    ) -> Protection | None:
        if keep_forever is None:
            details = tuple(
                row["detail"] for row in self.conn.execute(
                    "SELECT detail FROM protections WHERE kind = ? AND media_item_id = ?",
                    (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id),
                )
            )
        else:
            details = keep_forever.get(candidate.media_item_id, ())
        for detail in details:
            scope = parse_keep_forever_detail(detail)
            if scope is None:
                # A damaged scope names no unit and no set of seasons, so it
                # cannot be attributed to a row. Fail closed: the item stays
                # protected and the owner sees it in the Protected view,
                # instead of silently losing a "Keep forever" the app can no
                # longer read (S4 lead sentence, S6 anti-goal).
                logger.warning(
                    "keep_forever_scope_damaged",
                    extra={
                        "media_item_id": candidate.media_item_id,
                        "detail": detail,
                    },
                )
                return Protection(
                    kind=ProtectionKind.KEEP_FOREVER,
                    media_item_id=candidate.media_item_id,
                    detail=detail,
                )
            unit, seasons = scope
            if unit is not candidate.unit:
                continue
            if unit is Unit.SEASONS and set(seasons) != set(candidate.seasons):
                continue
            return Protection(
                kind=ProtectionKind.KEEP_FOREVER,
                media_item_id=candidate.media_item_id,
                detail=detail,
            )
        return None

    def _reason_parts(self, protection: Protection) -> tuple[text.ReasonPart, ...]:
        """The same sentence as a key plus raw parameters (#47)."""
        if protection.kind is ProtectionKind.LIBRARY:
            return (
                text.reason_part(
                    "protection.reason.library",
                    library=protection.detail or "",
                ),
            )
        if protection.kind is ProtectionKind.RATING:
            return (
                text.reason_part(
                    "protection.reason.rating",
                    rating=protection.detail or "",
                    minimum=self.settings.minimum_rating,
                ),
            )
        return (text.reason_part("protection.reason.keep_forever"),)
