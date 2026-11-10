# Version roadmap to 1.0.0

This app moves through pre-release versions to a publishable **1.0.0**. Versioning follows [SemVer](https://semver.org/) with one rule on top:

> **While pre-1.0, every completed ODD task bumps the MINOR number.** `0.MINOR.PATCH`.

- **MINOR** — one ODD task from `odd/tasks/almali-library-curator.md` completed and verified (suite green, task closed in the feature document).
- **PATCH** — a fix landed inside an already-tagged milestone before the next one opens (for example a bug fix on the current minor). No new capability.
- **1.0.0** — T15 release verification passes: full suite green, 5,000-movie / 300-series scan under 5 minutes, packaging (T12) and Unraid delivery (T13) done, docs (T14) complete, naming checks (S10) clean. That tag, and only that tag, is publishable.
- **After 1.0.0** — normal SemVer: new capability bumps MINOR, fixes bump PATCH.

## Milestones

Current version: **v0.29.0**. Next: T17 (field test on the owner's Unraid server) and T18 (the Almali Smart Cache interface redesign), each taking the next free minor number; **v1.0.0** (T15) comes only after T17 and T18.

| Version | ODD task | Status | Content |
| --- | --- | --- | --- |
| `v0.1.0` | T1 | Done | Foundation: package, settings, SQLite migrations, logging, i18n scaffolding, health endpoint, CI |
| `v0.2.0` | T2 | Done | Plex client and sync: paged rate-limited reads, models, library/season/episode/user/history sync |
| `v0.3.0` | T3 | Done | Optional connections: Sonarr, Radarr, Tautulli, TMDB, host filesystem — each degrading cleanly when absent |
| `v0.4.0` | T4 | Done | Detection engine: the five categories, global thresholds, follower/watched/mid-show, one-sentence reasons |
| `v0.5.0` | T5 | Done | Protection engine: protected library, high rating, kept forever; checked at scan and at re-check |
| `v0.6.0` | T6 | Done | Removal lifecycle: candidate → marked → Leaving soon → grace → final re-check → deleted → recorded |
| `v0.7.0` | T7 | Done | Verified deletion and history: delete via Sonarr/Radarr with files, unmonitor, import exclusion, disk-confirmed, hardlink-aware freed space |
| `v0.8.0` | T8 | Done | Scan orchestration: schedule + on demand, last-scan stamp, dry run, global kill switch |
| `v0.9.0` | T9 | Done | Web UI: Tabler + HUD layer, the approved mockup's screens |
| `v0.10.0` | T9a | Done | Plex connection from the app: address and token (or Plex sign-in) in the wizard and Connections, token encrypted at rest |
| `v0.11.0` | T9b | Done | Plex library selection: only the chosen libraries are synced and curated |
| `v0.12.0` | T9c | Done | Plex sign-in: the PIN flow at plex.tv as the primary connection, server discovery, manual entry kept as the alternative |
| `v0.13.0` | T9e | Done | Robustness: a damaged scope never breaks a screen, a seasons scope never falls back to the series, the stored failure is a translation key |
| `v0.14.0` | T9d | Done | Optional Sonarr and Radarr: both switchable, Plex the only required connection, Plex as the deletion fallback |
| `v0.15.0` | T10 | Done | i18n completion (en/ca/es) and accessibility pass |
| `v0.16.0` | T11 | Done | Security hardening: owner login, session cookie, sign-in lockout, CSRF on every action, log redaction |
| `v0.17.0` | T12 | Done | Docker image (multi-arch, non-root, health check) and compose example |
| `v0.18.0` | T13 | Done | Unraid installer, uninstaller, CA template with icon |
| `v0.19.0` | T14 | Done | Docs: README, CHANGELOG, disclaimers, naming checks |
| `v0.20.0` | T11b | Done | Sonarr, Radarr, Tautulli and TMDB keys entered in the app, encrypted at rest |
| `v0.21.0` | T16 | Done | Security hardening from the review advisories: session lifetime, global sign-out, bounded lockout, pinned CI, signed image check in the installer |
| `v0.22.0` | Issues #21, #24–#29 | Done | Quick UI fixes: first-run target size, server picker legend, disabled-control hint, consistent ca/es terms, new-tab Plex sign-in |
| `v0.23.0` | Issues #17, #22 | Done | Success and error flash messages: check or warning icon, status or alert role |
| `v0.24.0` | Issues #16, #18–#20, #23 | Done | Safety UX: Plex sign-in status polling, kill-switch badges, safeguards on every Library tab, confirmations, error pages without navigation |
| `v0.25.0` | Issues #30, #31, #33 | Done | Security: installer owner name without line breaks, no version in the public health check, owner-only settings and database |
| `v0.26.0` | Issue #32 | Done | Image dependencies from hash-pinned lock files |
| `v0.27.0` | Issues #11, #13, #14 | Done | Performance: larger Tautulli pages, batched file refresh, indexed scheduler lookup |
| `v0.28.0` | Issues #3, #10 | Done | Plex sync: bulk season and episode reads, 20 requests per second by default, batched writes |
| `v0.29.0` | Issues #4, #5, #6 | Done | Performance: detection groups events and episodes once per scan; single-item detection for the final re-check and for "Mark for removal" |
| `v1.0.0` | T15 | Planned | Release verification — **first publishable release** |

**T11b** (split from T11) shipped as v0.20.0. **T14a** (repository ready to publish: README, changelog, licence, security policy, contributing guide and templates; pulled forward from T14) shipped inside v0.16.0 and takes no number of its own.

`v0.10.0` sorts after `v0.9.0` as SemVer requires (numeric, not lexicographic). The two inserted tasks T9a and T9c shift the ladder; T15 stays `v1.0.0`.

## Tagging discipline

- Tags are annotated (`git tag -a`), named `v<version>`, created only by the orchestrator when a task closes, and pushed to `origin`.
- The version constants (`almali_library_curator/__init__.py` and `pyproject.toml`, kept in sync) are bumped in the same `chore:` commit the tag points at.
- Pre-1.0 tags are milestone markers; GitHub Releases start at `v1.0.0` (S8, T14). Every milestone is summarised in [`CHANGELOG.md`](../CHANGELOG.md).
- Tags follow the feature document: a tag exists only for a task whose row records its work-unit commits and green checks.
- The Almali rebrand (`odd/tasks/almali-rebrand.md`) is an identity change, not a capability milestone: it consumes no MINOR number and gets no tag, so the ladder above stays aligned to the v1 tasks T1–T15.
