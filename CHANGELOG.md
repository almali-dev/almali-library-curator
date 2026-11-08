# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While pre-1.0, every completed task bumps the minor version (see the
[roadmap](docs/ROADMAP.md)); the first publishable release is v1.0.0.

## [Unreleased]

### Security

- A session now ends 12 hours after the sign-in, however much it is used;
  using a session never extends it, and a session issued before this change
  (one with no sign-in time) counts as expired.
- **Sign out** now ends every session, in every browser: a session epoch in
  the config directory (owner-only, mode 0600, written atomically) is folded
  into every session, so a cookie copied before the sign-out is signed out
  afterwards. A missing, unreadable or corrupt epoch file never stops the
  app; the next sign-in or sign-out rewrites it.
- The brute-force guard keeps at most 1,024 client addresses in memory. The
  expired ones are cleaned up first and an address with an active lock is
  never evicted. The lockout thresholds and durations are unchanged.
- The publish workflow keeps manual dispatch but pushes `latest` only from
  `v*` tags; every action in `.github/workflows/*.yml` is pinned to a commit
  SHA and updated weekly by Dependabot.
- The Unraid installer asks for the **bind address** of the web port
  (`ALC_INSTALL_BIND`, `0.0.0.0` by default) and publishes the port on that
  address only; `127.0.0.1` keeps it private. The compose example takes the
  same variable.
- The Unraid installer verifies the **image signature** with `cosign` before
  creating anything (`ALC_INSTALL_REQUIRE_SIGNATURE=1` fails when `cosign`
  is absent), and waits (bounded, 120 s by default, `ALC_INSTALL_START_TIMEOUT`)
  for the container to report `healthy`, recording `start_verified` in the
  install manifest.

## [0.20.0] - 2026-11-08

### Added

- The Sonarr, Radarr, Tautulli and TMDB keys are entered in the app, on
  Connections and in the first-run wizard. Each key is tested against its
  service before it is stored, kept encrypted in `secrets.json` next to the
  Plex token, shown only masked, and can be forgotten.

### Changed

- A non-blank `ALC_` variable for a service (address or key) now manages that
  service alone: its form is replaced by a note and the in-app changes are
  refused. A blank variable counts as unset.

## [0.19.0] - 2026-11-08

### Added

- About page (`/about`, signed in) with the running version, the statement that
  the app is not affiliated with Plex Inc., the MIT licence and links to the
  repository, the changelog, the security policy and the issue tracker; every
  signed-in screen links to it.

## [0.18.0] - 2026-11-07

### Added

- Unraid delivery: `install-unraid.sh` (asks for the paths, port, ids, time zone
  and owner login, keeps the password in an owner-only env file, creates the
  session signing key, verifies its own published checksum and records what it
  created), `uninstall-unraid.sh` (removes only what the installer created and
  keeps the app data unless asked), the `unraid-template.xml` Community
  Applications template, and the Unraid deploy guide `docs/UNRAID.md`.
- A workflow that publishes the image to `ghcr.io/almali-dev/almali-library-curator`
  for amd64 and arm64 on every version tag, with the version and `latest` tags,
  signed with cosign.

## [0.17.0] - 2026-11-07

### Added

- Docker image built from this repository on `python:3.12-slim`: runs as
  `PUID`:`PGID` (default 99:100, root refused), fixes `/config` ownership at
  start, serves Gunicorn with one worker on port 17540, and has a health check
  on `/healthz`.
- `docker-compose.example.yml` for any Docker host. It requires the owner login
  (`ALC_AUTH_USER`, `ALC_AUTH_PASSWORD`) from a `.env` file and refuses to start
  without it.
- README section on running with Docker, including the reverse-proxy advice.

## [0.16.0] - 2026-11-07

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
- A successful sign-in no longer writes a user name that contains the password.
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

[Unreleased]: https://github.com/almali-dev/almali-library-curator/compare/v0.20.0...HEAD
[0.20.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.15.0...v0.16.0
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
