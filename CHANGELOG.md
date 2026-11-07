# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While pre-1.0, every completed task bumps the minor version (see the
[roadmap](docs/ROADMAP.md)); the first publishable release is v1.0.0.

## [Unreleased]

### Added

- Owner login from the environment (`ALC_AUTH_USER`, `ALC_AUTH_PASSWORD`): every
  screen and action needs it, there is no default, and the app answers HTTP 503
  while it is missing. `/healthz` reports `auth` as missing.
- Session cookie that is `HttpOnly`, `SameSite=Lax` and lasts 12 hours, with
  `ALC_SECURE_COOKIES=true` for HTTPS deployments; a sign-out button.
- Lockout of an address for 15 minutes after 5 failed sign-ins within 15 minutes.
- Log redaction: any structured field whose name looks like a password, token,
  secret, API key, authorization or cookie is written as `***`.
- `ALC_TRUSTED_PROXIES` configures the trusted reverse-proxy hop count used for the client address in lockout; the default remains `0`.
- A test that sends every mutating route without a CSRF token and expects a refusal.
- Repository documents: this changelog, the licence file, the security policy,
  the contributing guide and issue and pull request templates.

### Fixed

- Overlapping sign-ins now admit at most five password checks per address, and an in-flight success cannot clear a lock set while it was being checked.
- Log redaction now reaches every root handler, including handlers installed before logging was configured, and records from child loggers.
- A successful sign-in no longer writes a user name that also verifies as the password.
- Login lockout can distinguish clients behind a trusted reverse proxy without trusting `X-Forwarded-For` on a direct connection.

### Changed

- A session stays valid on every worker and after a restart, and changing the
  password signs it out.
- The signing key file must hold at least 32 bytes and is kept owner-only; a
  shorter key is replaced.

## [0.15.0] - 2026-11-06

### Added

- Complete English, Catalan and Spanish translations with no hard-coded strings.
- Accessibility pass: every screen usable at 320 px wide, 44 × 44 px touch
  targets, badges with text and an icon, no colour-only cue.

## [0.14.0] - 2026-11-06

### Added

- Sonarr and Radarr are optional and can each be switched off; Plex is the only
  required connection.
- Deletion through the Plex API when no Sonarr or Radarr handles the item.

## [0.13.0] - 2026-11-06

### Fixed

- A damaged removal scope no longer breaks a screen or the list of open removals.
- A seasons scope with no season rows is never widened to the whole series.
- A stored failure keeps a translation key, so it is shown in the reader's language.

## [0.12.0] - 2026-11-05

### Added

- Sign in with Plex: the PIN flow at plex.tv, discovery of your own servers, and
  manual entry kept as the alternative.

## [0.11.0] - 2026-11-05

### Added

- Curated libraries: only the Plex libraries you choose are synced, shown and curated.

## [0.10.0] - 2026-11-05

### Added

- Connect Plex from the first-run wizard and the Connections screen; the token is
  encrypted at rest and only its last four characters are shown.

## [0.9.0] - 2026-11-04

### Added

- Web UI: Library health with the Candidates, Leaving soon and Protected tabs,
  History with CSV export, Connections, Settings and the first-run wizard.
- CSRF protection on every mutating route and a persistent signing key.

## [0.8.0] - 2026-11-03

### Added

- Scan orchestration: a daily scan at a set time and scans on demand, one runner
  at a time, and the last-scan stamp.
- Dry run that reports what would happen without changing anything, and a global
  kill switch that stops every deletion at once.

## [0.7.0] - 2026-11-03

### Added

- Verified deletion through Sonarr or Radarr: files removed, unmonitored and
  excluded from import; freed space confirmed on disk and aware of hardlinks.
- Deletion history.

## [0.6.0] - 2026-11-03

### Added

- Removal lifecycle: mark, "Leaving soon" collection in Plex, grace period and a
  final re-check before deletion.

## [0.5.0] - 2026-11-03

### Added

- Protection engine: protected libraries, a minimum rating and items kept forever.

## [0.4.0] - 2026-11-03

### Added

- Detection engine: the five categories with global thresholds and a
  one-sentence reason per item.

## [0.3.0] - 2026-11-02

### Added

- Optional connections to Sonarr, Radarr, Tautulli, TMDB and the host
  filesystem, each degrading cleanly when absent.

## [0.2.0] - 2026-11-02

### Added

- Plex client and sync: paged, rate-limited reads of libraries, seasons,
  episodes, users and play history.

### Security

- Redirects are refused so the Plex token is never sent to another host.

## [0.1.0] - 2026-11-02

### Added

- Project foundation: package, settings, SQLite schema and migrations,
  structured logging, translations scaffolding, health endpoint and CI.

[Unreleased]: https://github.com/almali-dev/almali-library-curator/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/almali-dev/almali-library-curator/releases/tag/v0.1.0
