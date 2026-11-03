"""A failure-isolated optional enrichment pass, in dependency order."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import InitVar, dataclass, field

from almali_library_curator.config import Settings
from almali_library_curator.detection.enrichers import Enrichers
from almali_library_curator.enrichment.enrichers import build_enrichers
from almali_library_curator.enrichment.files import FileSummary, refresh_files
from almali_library_curator.enrichment.history import import_tautulli_history
from almali_library_curator.enrichment.linking import LinkSummary, link_arr
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from almali_library_curator.tautulli import TautulliClient
from almali_library_curator.tmdb import TmdbClient

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentSummary:
    """Serializable counts/errors plus transient, scan-scoped detection adapters.

    ``enrichers`` is an InitVar deliberately: asdict() serializes audit facts,
    never live clients, credentials, connections or caches. The attribute is
    available to the scan pipeline for detection and its final re-check.
    """

    linking: LinkSummary | None = None
    files: FileSummary | None = None
    history_imported: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    enrichers: InitVar[Enrichers | None] = None

    def __post_init__(self, enrichers: Enrichers | None) -> None:
        self.enrichers = enrichers or Enrichers()


def _configured(client, client_type, settings, service, credential):
    if client is not None:
        return client
    if getattr(settings, f"{service}_base_url") and getattr(settings, credential):
        return client_type.from_settings(settings)
    return None


def run_enrichment(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    sonarr: SonarrClient | None = None,
    radarr: RadarrClient | None = None,
    tautulli: TautulliClient | None = None,
    tmdb: TmdbClient | None = None,
    hostfs: HostFilesystem | None = None,
) -> EnrichmentSummary:
    """Link arr identities, refresh files, then import longer play history.

    Missing services are optional. Failures are class names only: service
    error messages are external input and may contain credentials. TMDB
    detection adapters are lazy and perform no title searches.
    """
    summary = EnrichmentSummary()

    def failed(step, error):
        summary.errors[step] = type(error).__name__
        logger.warning("enrichment_step_failed", extra={"step": step, "error_type": type(error).__name__})

    clients = {}
    for name, client, client_type in (("sonarr", sonarr, SonarrClient), ("radarr", radarr, RadarrClient)):
        try:
            clients[name] = _configured(client, client_type, settings, name, f"{name}_api_key")
        except Exception as error:
            failed(f"link_arr.{name}", error)
    try:
        summary.linking = link_arr(conn, **clients)
        for name, error_type in summary.linking.errors.items():
            summary.errors[f"link_arr.{name}"] = error_type
    except Exception as error:
        failed("link_arr", error)

    try:
        summary.files = refresh_files(conn, hostfs if hostfs is not None else HostFilesystem.from_settings(settings))
    except Exception as error:
        failed("refresh_files", error)

    try:
        tautulli = _configured(tautulli, TautulliClient, settings, "tautulli", "tautulli_api_key")
        summary.history_imported = import_tautulli_history(conn, tautulli)
    except Exception as error:
        failed("import_tautulli_history", error)

    try:
        if tmdb is None and settings.tmdb_api_token:
            tmdb = TmdbClient.from_settings(settings)
        summary.enrichers = build_enrichers(conn, tmdb=tmdb)
    except Exception as error:
        failed("build_enrichers", error)
    logger.info("enrichment_finished", extra={"history_imported": summary.history_imported,
                                              "failed_steps": sorted(summary.errors)})
    return summary
