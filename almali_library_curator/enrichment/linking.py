"""Conservative, read-only-service identity linking for deletion targets."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from almali_library_curator import library_scope
from almali_library_curator.config import Settings
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient

logger = logging.getLogger(__name__)


@dataclass
class LinkSummary:
    sonarr_linked: int = 0
    radarr_linked: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    skipped_services: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


def _index(resources, attribute):
    index = {}
    for resource in resources:
        value = getattr(resource, attribute)
        if value:
            index.setdefault(value, []).append(resource)
    return index


def _scoped_rows(conn, item_type: str, allowed: frozenset[int] | None) -> bool:
    """Whether any item of the type is inside the curated scope."""
    if allowed is None:
        return True
    if not allowed:
        return False
    marks = ", ".join("?" for _ in allowed)
    return conn.execute(
        f"SELECT 1 FROM media_items WHERE item_type = ? AND id IN ({marks}) LIMIT 1",
        (item_type, *sorted(allowed)),
    ).fetchone() is not None


def _link_service(conn, service, resources, summary, allowed: frozenset[int] | None = None):
    is_movie = service == "radarr"
    primary_column, primary_attribute = ("tmdb_id", "tmdbId") if is_movie else ("tvdb_id", "tvdbId")
    primary = _index(resources, primary_attribute)
    secondary = _index(resources, "imdbId") if is_movie else {}
    titles = {}
    for resource in resources:
        if resource.title and resource.year is not None:
            titles.setdefault((resource.title.casefold(), resource.year), []).append(resource)
    sql = "SELECT * FROM media_items WHERE item_type=?"
    params: list = ["movie" if is_movie else "series"]
    if allowed is not None:
        if not allowed:
            return  # nothing of this type is inside the curated scope
        marks = ", ".join("?" for _ in allowed)
        sql += f" AND id IN ({marks})"
        params += sorted(allowed)
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        matches = primary.get(row[primary_column], [])
        if not matches and is_movie and row["imdb_id"]:
            matches = secondary.get(row["imdb_id"], [])
        if not matches and not any(row[key] for key in ("tmdb_id", "tvdb_id", "imdb_id")):
            matches = titles.get((row["title"].casefold(), row["year"]), [])
        match = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            summary.ambiguous += 1
        elif match is None:
            summary.unmatched += 1
        if match is not None:
            if is_movie:
                summary.radarr_linked += 1
            else:
                summary.sonarr_linked += 1
        # Refresh rather than retain a stale or newly ambiguous deletion target.
        conn.execute(f"UPDATE media_items SET {service}_id=?, monitored=? WHERE id=?",
                     (match.id if match else None,
                      int(match.monitored) if match else None, row["id"]))
        if not is_movie:
            status = None
            if match is not None:
                status = (match.status or "").lower()
                if not status and match.ended is not None:
                    status = "ended" if match.ended else "continuing"
            conn.execute("UPDATE media_items SET production_status=? WHERE id=?",
                         (status or None, row["id"]))


def link_arr(conn: sqlite3.Connection, *, sonarr: SonarrClient | None = None,
             radarr: RadarrClient | None = None, settings: Settings | None = None) -> LinkSummary:
    """Link by authoritative id, or exact title/year only with no known id.

    An ambiguous primary identity never falls through to a weaker identity.
    An absent service preserves existing links; an answered inventory refreshes
    them, clearing missing and ambiguous matches. Each service is isolated.

    Only items inside the curated scope are linked (S14): a deselected
    library's items are not sent to any service, keep their existing links
    exactly like an unconfigured service, and a service whose items are all
    out of scope is not contacted at all. ``settings=None`` (or an empty
    curated selection) means no restriction, as today.
    """
    summary = LinkSummary()
    allowed = library_scope.curated_item_ids(conn, settings) if settings is not None else None
    for name, client in (("sonarr", sonarr), ("radarr", radarr)):
        if client is None:
            summary.skipped_services += (name,)
            continue
        if not _scoped_rows(conn, "movie" if name == "radarr" else "series", allowed):
            logger.info("enrichment_link_skipped_out_of_scope", extra={"service": name})
            continue
        try:
            resources = client.all_series() if name == "sonarr" else client.all_movies()
            partial = LinkSummary()
            with conn:
                _link_service(conn, name, resources, partial, allowed=allowed)
            summary.sonarr_linked += partial.sonarr_linked
            summary.radarr_linked += partial.radarr_linked
            summary.ambiguous += partial.ambiguous
            summary.unmatched += partial.unmatched
        except Exception as error:
            summary.errors[name] = type(error).__name__
            logger.warning("enrichment_link_failed", extra={"service": name, "error_type": type(error).__name__})
    return summary
