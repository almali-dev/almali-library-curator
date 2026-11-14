"""Production scan boundaries; optional enrichment is loaded only on use."""
from dataclasses import asdict, is_dataclass
import re

from almali_library_curator.detection.engine import DetectionEngine
from almali_library_curator.lifecycle import DetectionRematcher, LifecycleEngine
from almali_library_curator.lifecycle.deletion import DeletionExecutor
from almali_library_curator.plex import PlexClient, PlexSync
from almali_library_curator.protection import ProtectionEngine
from almali_library_curator.radarr import RadarrClient
from almali_library_curator.sonarr import SonarrClient
from almali_library_curator.tautulli import TautulliClient
from almali_library_curator.tmdb import TmdbClient
from almali_library_curator.hostfs import HostFilesystem
from almali_library_curator.scan.snapshot import detection_snapshot


class RecheckFailed(RuntimeError):
    """At least one final check could not be completed safely."""


def default_enrichment(conn, settings, **services):
    from almali_library_curator.enrichment import run_enrichment
    return run_enrichment(conn, settings, **services)


def counts(summary):
    """Keep counts and diagnostic class names, never external error bodies."""
    if is_dataclass(summary):
        summary = asdict(summary)
    if not isinstance(summary, dict):
        return {}
    result = {}
    for key, value in summary.items():
        if key == "errors" and isinstance(value, dict):
            result[key] = {step: diagnostic for step, diagnostic in value.items()
                           if isinstance(diagnostic, str) and re.fullmatch(
                               r"[A-Za-z_][A-Za-z0-9_]*", diagnostic)}
        elif isinstance(value, (int, float)):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = counts(value)
    return result


def build_detection(conn, settings, clock, *, tmdb=None):
    """One enrichment/protection configuration for scans and mark-time checks."""
    from almali_library_curator.enrichment.enrichers import build_enrichers
    if tmdb is None and settings.tmdb_api_token:
        tmdb = TmdbClient.from_settings(settings)
    return DetectionEngine(conn, settings=settings, clock=clock,
                           enrichers=build_enrichers(conn, tmdb=tmdb),
                           protector=ProtectionEngine(conn, settings=settings))


def default_steps(conn, settings, clock, enrichment_step):
    plex = PlexClient.from_settings(settings)
    sonarr = SonarrClient.from_settings(settings) if settings.sonarr_base_url else None
    radarr = RadarrClient.from_settings(settings) if settings.radarr_base_url else None
    tautulli = TautulliClient.from_settings(settings) if settings.tautulli_base_url else None
    tmdb = TmdbClient.from_settings(settings) if settings.tmdb_api_token else None
    hostfs = HostFilesystem.from_settings(settings)
    protection = ProtectionEngine(conn, settings=settings)
    detection = build_detection(conn, settings, clock, tmdb=tmdb)
    lifecycle = LifecycleEngine(conn, settings=settings, clock=clock,
                                collections=plex, protection=protection,
                                rematcher=DetectionRematcher(detection))
    executor = DeletionExecutor(lifecycle, sonarr=sonarr, radarr=radarr, plex=plex,
                                hostfs=hostfs)
    passed_ids = []

    def sync():
        from almali_library_curator.enrichment.sources import invalidate_disks
        # Sync can reassign a library before file refresh runs. Invalidate
        # first, including when a later sync/refresh fails; renderers then
        # query current facts until enrichment commits their replacement.
        with conn:
            invalidate_disks(conn)
        return counts(PlexSync(plex, conn, curated_libraries=settings.curated_libraries).run())

    def enrich():
        summary = (enrichment_step or default_enrichment)(
            conn, settings, sonarr=sonarr, radarr=radarr, tautulli=tautulli,
            tmdb=tmdb, hostfs=hostfs,
        )
        # A provider may also return the concrete detection adapters. Never
        # import optional implementation classes merely to construct a runner.
        enrichers = getattr(summary, "enrichers", None)
        if enrichers is not None:
            detection.enrichers = enrichers
        return counts(summary)

    def detect():
        result = detection.scan_with_protection()
        return detection_snapshot(conn, result)

    def recheck():
        report = lifecycle.run_recheck()
        if report.errors:
            raise RecheckFailed()
        passed_ids[:] = [outcome.request.id for outcome in report.passed]
        return {"passed": len(report.passed), "cancelled": len(report.cancelled),
                "outcomes": [{"request_id": o.request.id,
                              "status": ("would_" if settings.dry_run else "") + (
                                  "pass" if o.passed else "cancel"),
                              "reason": o.cancel_reason} for o in report.outcomes]}

    def delete():
        report = executor.run_deletions(passed_ids=passed_ids)
        return {"outcomes": [{"request_id": o.request_id, "status": o.status.value,
                              "size_bytes": o.size_bytes, "freed_bytes": o.freed_bytes,
                              "files": list(o.files), "error": o.error}
                             for o in report.outcomes],
                "count": len(report.outcomes)}

    return {"sync": sync,
            "enrichment": enrich, "detection": detect, "recheck": recheck,
            "deletion": delete}
