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


def _format_rating(value: float) -> str:
    """A rating like 8.7 or 8.0, for interpolation into reasons."""
    return str(float(value))


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
        survivors: list[Candidate] = []
        protected: list[ProtectedCandidate] = []
        for candidate in candidates:
            protection = self._protection_for(candidate, protected_libraries)
            if protection is None:
                survivors.append(candidate)
            else:
                protected.append(
                    ProtectedCandidate(
                        candidate=candidate,
                        protection=protection,
                        reason=self._reason(protection),
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
        self, candidate: Candidate, protected_libraries: dict[int, str]
    ) -> Protection | None:
        """One item's protection, in the S4 precedence order: library,
        then rating, then kept forever."""
        library = self._library_protection(candidate, protected_libraries)
        if library is not None:
            return library
        rating = self._rating_protection(candidate)
        if rating is not None:
            return rating
        return self._keep_forever_protection(candidate)

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

    def _library_protection(
        self, candidate: Candidate, protected_libraries: dict[int, str]
    ) -> Protection | None:
        row = self.conn.execute(
            "SELECT library_id FROM media_items WHERE id = ?",
            (candidate.media_item_id,),
        ).fetchone()
        if row is None or row["library_id"] is None:
            return None
        library_id = int(row["library_id"])
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
            detail=_format_rating(candidate.rating),
        )

    def _keep_forever_protection(self, candidate: Candidate) -> Protection | None:
        rows = self.conn.execute(
            "SELECT detail FROM protections WHERE kind = ? AND media_item_id = ?",
            (ProtectionKind.KEEP_FOREVER.value, candidate.media_item_id),
        ).fetchall()
        for row in rows:
            scope = parse_keep_forever_detail(row["detail"])
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
                        "detail": row["detail"],
                    },
                )
                return Protection(
                    kind=ProtectionKind.KEEP_FOREVER,
                    media_item_id=candidate.media_item_id,
                    detail=row["detail"],
                )
            unit, seasons = scope
            if unit is not candidate.unit:
                continue
            if unit is Unit.SEASONS and set(seasons) != set(candidate.seasons):
                continue
            return Protection(
                kind=ProtectionKind.KEEP_FOREVER,
                media_item_id=candidate.media_item_id,
                detail=row["detail"],
            )
        return None

    def _reason(self, protection: Protection) -> str:
        """The localized "what protects it" the Protected view shows."""
        if protection.kind is ProtectionKind.LIBRARY:
            return text.render(
                self.locale,
                "protection.reason.library",
                library=protection.detail or "",
            )
        if protection.kind is ProtectionKind.RATING:
            return text.render(
                self.locale,
                "protection.reason.rating",
                rating=protection.detail or "",
                minimum=_format_rating(self.settings.minimum_rating),
            )
        return text.render(self.locale, "protection.reason.keep_forever")
