# Version roadmap to 1.0.0

This app moves through pre-release versions to a publishable **1.0.0**. Versioning follows [SemVer](https://semver.org/) with one rule on top:

> **While pre-1.0, every completed ODD task bumps the MINOR number.** `0.MINOR.PATCH`.

- **MINOR** — one ODD task completed and verified (suite green, task closed in the feature document).
- **PATCH** — a fix landed inside an already-tagged milestone before the next one opens (for example a bug fix on the current minor). No new capability.
- **1.0.0** — T15 release verification passes: full suite green, 5,000-movie / 300-series scan under 5 minutes, packaging (T12) and Unraid delivery (T13) done, docs (T14) complete, naming checks (S10) clean. That tag, and only that tag, is publishable.
- **After 1.0.0** — normal SemVer: new capability bumps MINOR, fixes bump PATCH.

## Milestones

| Version | ODD task | Content |
| --- | --- | --- |
| `v0.1.0` | T1 | Foundation: package, settings, SQLite migrations, logging, i18n scaffolding, health endpoint, CI |
| `v0.2.0` | T2 | Plex client and sync: paged rate-limited reads, models, library/season/episode/user/history sync |
| `v0.3.0` | T3 | Optional connections: Sonarr, Radarr, Tautulli, TMDB, host filesystem — each degrading cleanly when absent |
| `v0.4.0` | T4 | Detection engine: the five categories, global thresholds, follower/watched/mid-show, one-sentence reasons |
| `v0.5.0` | T5 | Protection engine: protected library, high rating, kept forever; checked at scan and at re-check |
| `v0.6.0` | T6 | Removal lifecycle: candidate → marked → Leaving soon → grace → final re-check → deleted → recorded |
| `v0.7.0` | T7 | Verified deletion and history: delete via Sonarr/Radarr with files, unmonitor, import exclusion, disk-confirmed, hardlink-aware freed space |
| `v0.8.0` | T8 | Scan orchestration: schedule + on demand, last-scan stamp, dry run, global kill switch |
| `v0.9.0` | T9 | Web UI: Tabler + HUD layer, the approved mockup's screens |
| `v0.10.0` | T9a | Plex connection from the app: address and token (or Plex sign-in) in the wizard and Connections, token encrypted at rest |
| `v0.11.0` | T9b | Plex library selection: only the chosen libraries are synced and curated |
| `v0.12.0` | T9c | Plex sign-in: the PIN flow at plex.tv as the primary connection, server discovery, manual entry kept as the alternative |
| `v0.13.0` | T9d | Optional Sonarr and Radarr: both switchable, Plex the only required connection, Plex as the deletion fallback |
| `v0.14.0` | T10 | i18n completion (en/ca/es) and accessibility pass |
| `v0.15.0` | T11 | Security hardening: login, CSRF, encrypted secrets for the remaining services, log redaction |
| `v0.16.0` | T12 | Docker image (multi-arch, non-root, health check) and compose example |
| `v0.17.0` | T13 | Unraid installer, uninstaller, CA template with icon |
| `v0.18.0` | T14 | Docs: README, CHANGELOG, disclaimers, naming checks |
| `v1.0.0` | T15 | Release verification — **first publishable release** |

`v0.10.0` sorts after `v0.9.0` as SemVer requires (numeric, not lexicographic). The two inserted tasks T9a and T9c shift the ladder; T15 stays `v1.0.0`.

## Tagging discipline

- Tags are annotated (`git tag -a`), named `v<version>`, created only by the orchestrator when a task closes, and pushed to `origin`.
- The version constants (`almali_library_curator/__init__.py` and `pyproject.toml`, kept in sync) are bumped in the same `chore:` commit the tag points at.
- Pre-1.0 tags are milestone markers; release notes and GitHub Releases start at `v1.0.0` (S8, T14).
- Tags follow the feature document: a tag exists only for a task whose row records its work-unit commits and green checks.
- The Almali rebrand is an identity change, not a capability milestone: it consumes no MINOR number and gets no tag, so the ladder above stays aligned to the v1 tasks T1–T15.
