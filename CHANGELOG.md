# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While pre-1.0, every completed task bumps the minor version (see the
[roadmap](docs/ROADMAP.md)); the first publishable release is v1.0.0.

## [Unreleased]

## [0.37.0] - 2026-11-16

### Changed

- The build and development tools are up to date: pytest 9.2.0,
  pytest-xdist 3.8.0, PyYAML 6.0.3, ruff 0.17.4, pyright 1.1.414 and
  `docker/build-push-action` v7.4.0. The release workflow pins
  `sigstore/cosign-installer` v4.1.3 but keeps installing cosign v2.6.5,
  so the existing signature checks keep working (#92-#98).
- The unused `pydantic-settings` dependency is removed and the Docker
  lock regenerated, so the image no longer ships it or `python-dotenv`;
  packaging tests now refuse a runtime dependency the package never
  imports (#105).

### Fixed

- Restoring or stopping the monitoring of a series or its seasons in
  Sonarr writes only the monitoring flags, through the season pass and
  the series editor, instead of the whole series, so a change another
  client made in the meantime is no longer written back (#102).
- Cancelling a failed removal reverts only what the removal changed: an
  import exclusion that already existed, or a title or season the owner
  had already unmonitored, is left as it was. An exclusion created by an
  earlier attempt of the same removal is still removed (#103).
- An upgrade with the Unraid installer checks the app data path recorded
  in its manifest like a typed answer, so a recorded path that is
  relative or holds `:` or `,` is refused before anything changes (#104).

## [0.36.0] - 2026-11-16

### Added

- The language can be chosen in **Settings** (English, Català, Español)
  and through `ALC_LOCALE` in the Unraid template; the template ships the
  variable empty so the Settings choice applies, a set value wins, and an
  unsupported one is refused (#47).

### Fixed

- Reasons are stored as a key plus raw parameters and rendered in the
  reader's language: Library and Protected reasons, cancellation reasons on
  Leaving soon, and History and the CSV export follow the selected language,
  including dates. Rows written by earlier versions keep showing their
  stored text. Migration 013 adds the nullable `reason_parts` and
  `cancel_params` columns; the database is backed up automatically before
  it runs (#47).
- The Library and Protected tabs show 50 rows per page (up to 100 with
  `per_page`), with sorting, filters and counts done in SQL, and only the
  tab being viewed is built. Migration 014 adds an indexed display cache of
  the latest published scan, filled from it on upgrade and kept in step by
  the database (#48).
- The Plex sync reads and stores the library page by page instead of
  holding it all in memory, still publishing the snapshot in one
  transaction (#60).
- A scan no longer stats every media file: files whose Plex path and size
  are unchanged are skipped, and the disk is cached per directory and the
  last-known disk probed first, so the array is not woken for nothing (#58).

## [0.35.0] - 2026-11-15

### Fixed

- A cancellation now claims the removal (a transient `cancelling` state)
  before it reverts the arr side effects of a failed removal, so a retry
  can no longer claim and delete it while the revert runs; a cancellation
  that lost the race sends no arr request at all (#99).
- The revert reads the applied-effects record live, under the write lock
  that takes the claim, and undoes exactly what it names (#100). A claim
  left behind by a crash returns to "Removal failed" at the first scan
  30 minutes later.
- The Radarr and Sonarr clients keep only their 256 most recent requests
  in memory instead of every request of a long-lived client (#101).
- The periodic VACUUM waits one second for the writer slot instead of
  30 s: a held writer defers the rebuild to the next maintenance boundary
  instead of stalling Mark, Keep and the kill switch (#84).
- The `plex_sync_finished` log line carries the publishing mode (`own` or
  `savepoint`), so a sync whose caller later rolled back is diagnosable
  (#90).

- The installer writes its manifest with a checked write and a size
  check: a failed or partial write (a full app data share) leaves the
  previous manifest untouched and aborts the run (#86).
- An upgrade whose new container name was taken by another installation
  keeps the manifest, environment file, signing key and app data, so the
  uninstaller can still verify ownership and clean up (#88, #89).
- The uninstaller reports docker's own error when `docker inspect` fails
  during the ownership check, instead of "no ownership label" (#91).

- History dates follow the selected language, the CSV export translates
  its header row in Catalan and Spanish (English keeps the published field
  names), and the language switch never links to a POST-only URL after a
  form submission (#73).
- Accessibility: every page has its own title, each library checkbox is
  labelled by its library, the setup navigations have distinct labels,
  section titles and buttons are at least 12 px, form controls get a
  stronger border, buttons show a visible focus ring, settings fields carry
  help text and a rejected form lists its errors in a summary (#72).

### Tests

- The signing-key guarantees of `docs/UNRAID.md` and the upgrade-path
  container-name race are pinned by tests (#85, #89).
- A Plex fetch failure before the sync savepoint keeps the caller's
  pending work (#87); the migration-chain test reads the fully migrated
  database through the public read paths (#68).

## [0.34.0] - 2026-11-14

### Fixed

- Cancelling a removal whose deletion already failed after the arr was
  mutated no longer leaves the title unmonitored and excluded: the
  executor records every arr side effect it applies, and every
  cancellation — the owner's Undo and Keep it, and the final check's own
  exits — reverts them through the arr clients first, reporting a
  revert it could not finish instead of claiming it (issue #69).
- A paged Plex read no longer stops after the first page when the server
  omits `totalSize`: the read continues until a page comes back short, so
  recent plays and collections on later pages are no longer lost, and a
  page that repeats refuses instead of looping (issue #51).
- The Unraid installer validates the host paths it binds instead of trusting
  the manifest: the media path of an `--upgrade` passes the same check as the
  prompted answer (an absolute path), and no host path with `:` or `,` is
  accepted, either when asked or when read from the manifest. Docker splits a
  bind on those characters, so the container could have started with the wrong
  `/media`; the installer now refuses before it stops, archives or binds
  anything (issue #83).
- A deletion adds the arr import exclusion through the endpoint the server
  really serves and never posts one that is already there, so a Radarr
  deletion stops failing after the unmonitor and a retry after a failed
  delete succeeds for both Radarr and Sonarr (issue #52).
- A season added inside the stalled-series window is no longer a stalled
  candidate, and a Plex history entry counts as watched even when the
  server reports no playback position, so the "not rewatched" and
  "everyone finished" rules work without Tautulli (issue #70).

## [0.33.0] - 2026-11-14

### Fixed

- The Unraid installer recovers a failed run instead of leaving a half
  installed service behind: it keeps an owner-only backup, records what it
  did in its manifest and drives an upgrade from that manifest, so the
  replacement container keeps the labels, the mounts and the signature
  verification of the installed one. An upgrade that cannot archive every
  configured data path refuses before it touches anything, and neither the
  installer nor the uninstaller ever removes a container that belongs to
  another installation (issues #43, #53, #54).
- A deletion never claims more than it proved: the marked files are checked
  against the arr that owns them before any mutation, and a file that was
  never seen is never reported as deleted (issues #38, #39).
- The dry-run preview now tells the same outcome as the real run on the
  Plex route, so what a preview promises is what the run does (issues #38,
  #39).
- A failed enrichment step blocks the deletion pass for that run: a failed
  Tautulli import or arr linking no longer lets the run delete what it could
  not check (issue #49).
- The database migrates atomically: every migration and its schema version
  commit together, a copy of the database is taken before the first
  migration, a newer schema refuses to open instead of being downgraded, and
  a migration never commits work the caller left pending (issue #40).
- Items Plex no longer knows about leave detection and removal scope
  instead of being kept forever, but only after a complete section read
  proves their absence; an incomplete read leaves them untouched (issue
  #50).
- A season container that is still present keeps its removal request open,
  even when all its episodes are gone (issue #50).
- The Plex browser picker never blocks on a blank token: the page reports
  the empty token instead of waiting forever, while the server still refuses
  a token sent from a changed address (issue #61).
- The scan report no longer grows without bound: the newest runs keep a
  materialized display projection with every detection section, the old full
  reports are pruned, the page reads labels instead of whole rows, and the
  Plex sync fetches everything before it writes, joining an already open
  transaction without ever committing the caller's pending work. The
  `report` key of `last_scan` now carries the short summary of the run steps
  instead of the whole report, so a caller that read it must read the
  summary, not the payload (issues #57, #59).
- The Plex file path and size are read from `Media[].Part[]`, the shape the
  server really returns; the fake server in the tests serves that same
  shape, so the suite can no longer pass against a payload the server never
  sends (issue #37).
- The first run is a real step by step: the wizard explains each step and
  the outcome of a scan, successful or failed, is visible instead of
  silently dead-ending (issues #45, #46).
- The Plex fallback refuses a multi-version item before it deletes any
  metadata, instead of deleting the metadata of one version and keeping the
  item (found in the 0.34 field test).
- The Plex authorization link carries the product context Plex requires,
  and the product context keeps its separator literal (found in the 0.34
  field test).

### Changed

- Releases are cut from `main` and only after CI is green; publishing is
  concurrent-safe, an existing version is never mutated, and `latest` only
  points at a stable release (issues #41, #55).
- CI gates lint (ruff) and types (pyright) with a documented baseline, runs
  the browser-layout and workflow tests with their extras installed, fails
  when one of those optional tests would be skipped, and the container job
  installs what the shared test configuration imports (issue #67).
- The documentation matches the code: the `/config` volume holds settings,
  the database, secrets and backups while logs go to the container output;
  the issue forms point to the About page for the version; SECURITY.md names
  every encrypted credential, states that credentials given through
  environment variables are not encrypted and that `/healthz` is public
  (issue #66).

### Added

- A migration-chain test from a populated v1 database with failures at the
  first and at a middle step, and an HTTP end-to-end test of the whole scan,
  mark, grace, scan, verified delete and History cycle; the fake servers poll
  faster, which shortens a serial run of the suite (issue #68).

### Security

- The web app sends security headers, the session cookie has its own name
  and a changed server address no longer keeps a stored credential
  (issues #61, #62).

## [0.32.0] - 2026-11-11

### Fixed

- The detection engine asks for the series status again on every detection
  pass instead of keeping the first pass's answer for the life of the
  engine, so a reused engine sees a series that was cancelled or finished
  since the last pass; the re-check uses that answer when it decides
  whether a removal request still matches (issue #34).
- The host filesystem disk-usage test no longer compares two live readings
  of the same disk for equality: it brackets the adapter's reading between
  them, so the test no longer fails when the machine writes to that disk
  while the test runs (issue #35).

### Added

- The sources panel has an end-to-end test for the whole composition: a real
  sync invalidates the recorded disk sets, the file refresh records them
  again, and the panel then answers from the recorded value.
- The episode-play cancellation test derives its expected day from its frozen
  clock instead of a literal, and covers a play that crosses midnight.


## [0.31.0] - 2026-11-10

### Security

- The History CSV export also escapes cells that start with a tab or a
  carriage return; the formula-injection guard stripped them before checking
  the prefixes, so they were never escaped.

### Changed

- The sources panel reads the disks recorded by the last file refresh instead
  of scanning every media item and episode on each render; a missing or
  invalid record falls back to the live query.
- Each detection pass reads the users once, and a reused detection engine
  sees user changes on the next pass instead of keeping them for its lifetime.

### Added

- A regression test pins that a new play on an episode cancels a seasons
  removal request; the behavior was already in place.

## [0.30.0] - 2026-11-10

### Changed

- The Library page builds the candidates, the protected items and the leaving
  requests once per request and parses the scan report once, instead of
  building every view twice and parsing the whole report again for each of
  them; a render with 2,541 candidates spent 243 ms and 372 SQL statements,
  about half of it duplicated.
- A scan reads the users once, and the protection pass reads the library and
  the "Keep forever" scopes of the items once, instead of asking the database
  again for every candidate: 9,549 statements per scan at 3,501 candidates
  become a handful of queries.
- The History page shows the newest events in pages of fifty, sorted on an
  index that serves the order of the instant they happened, and the CSV
  export streams its rows instead of building the whole file in memory.

## [0.29.0] - 2026-11-10

### Changed

- A detection scan groups the play events and the episode index once instead
  of filtering the whole history once per series and once per movie, so the
  scan no longer slows down with the length of the Tautulli history; the
  detected candidates are the same.
- The final re-check of a due request and the "Mark for removal" action detect
  only the media item they concern, instead of running a full detection and
  protection scan per request and per click; a single-item detection applies
  the same library scope, users, watch thresholds and protection rules as the
  full scan and always reads the current database.

## [0.28.0] - 2026-11-09

### Changed

- A full Plex sync reads each show library's seasons and episodes in bulk
  instead of one request per series and per season, falling back to the
  per-item reads when Plex answers a bulk read with nothing; the synced data
  is the same.
- The default Plex rate limit rises from 5 to 20 requests per second for a
  server on the local network (`ALC_PLEX_RATE_LIMIT_PER_SECOND` or a stored
  value still wins); retries, backoff, paging and the redirect refusal are
  unchanged.
- The sync prefetches the season and user maps once and writes its rows in
  batches instead of tens of thousands of single-row statements.

## [0.27.0] - 2026-11-09

### Changed

- Tautulli history is imported 1,000 rows per request instead of 25, with the
  same rows, paging and retries.
- The file refresh writes the size, hardlink count and disk of up to 1,000
  files per batched statement instead of one statement per file.
- The scheduler finds today's scheduled run with an indexed, bounded query;
  database migration 6 adds the index.

## [0.26.0] - 2026-11-09

### Security

- The container image installs its Python dependencies from committed,
  hash-pinned lock files (`docker/requirements.lock`,
  `docker/build-requirements.lock`) with `pip --require-hashes`, and the
  runtime stage installs offline from the verified wheels, so a changed
  package on the index cannot enter a signed image.

### Changed

- The major-version caps of the dependencies moved from
  `docker/constraints.txt` into `pyproject.toml`, so the package and the image
  declare the same bounds; Dependabot watches both.

## [0.25.0] - 2026-11-09

### Security

- The Unraid installer refuses an owner name with a line break, as it already
  did for the password, before pulling an image or writing any app data, so
  the name cannot add lines to the container's environment file.
- The public `/healthz` answer shows only liveness and readiness, no longer
  the application or database schema version; the signed-in About page still
  shows the version.
- `settings.json`, the SQLite database and its `-wal`/`-shm` sidecars are
  created owner-only (mode 0600) whatever the umask; a file owned by another
  user keeps its mode rather than stopping the app.

## [0.24.0] - 2026-11-09

### Fixed

- A pending Plex sign-in now refreshes only its status region instead of
  reloading the page every 3 seconds, so a manually entered address or token
  is kept; **Check again** still reloads it without JavaScript.
- Settings shows dedicated kill-switch on and off badges, every Library tab
  shows the active dry-run and kill-switch safeguards, and the re-check line
  no longer claims a blocked deletion is awaiting removal.
- Resuming deletions and forgetting the Plex token now ask for an explicit
  confirmation; error pages no longer show the sidebar navigation.

## [0.23.0] - 2026-11-09

### Changed

- Successful owner actions now show a check-marked status message, while action
  failures show a warning-marked alert without changing the message text.

## [0.22.0] - 2026-11-09

### Fixed

- First-run setup, Plex server selection and disabled library protection controls now provide accessible targets, labels and guidance; Plex sign-in opens separately without weakening its existing outbound-link protection.
- Catalan and Spanish category and navigation labels now use consistent terminology.

## [0.21.0] - 2026-11-09

### Security

- A session now ends 12 hours after the sign-in, however much it is used;
  using a session never extends it, and a session issued before this change
  (one with no sign-in time) counts as expired.
- **Sign out** now ends every session, in every browser: a session epoch in
  the config directory (owner-only, mode 0600, written atomically) is folded
  into every session, so a cookie copied before the sign-out is signed out
  afterwards. A missing, unreadable or corrupt epoch file never stops the
  app; the next sign-in or sign-out rewrites it. If the epoch cannot be
  advanced during sign-out, this browser's session is still cleared, the
  previous epoch stays in force, and the request answers HTTP 503 with a
  translated error; other sessions remain valid until a later sign-out or
  their expiry.
- The brute-force guard keeps at most 1,024 client addresses in memory. The
  expired ones are cleaned up first, and an address with unexpired failed
  attempts or an active lock is never evicted, so cycling other addresses
  cannot reset its count. The lockout thresholds and durations are unchanged.
  If every slot holds such live state, new unseen addresses are refused with
  HTTP 429 until a slot frees.
- The publish workflow keeps manual dispatch, but a branch dispatch compiles
  both architectures without logging in, pushing or signing. Only
  `refs/tags/v*` pushes the version and `latest` tags and signs the image;
  every action in `.github/workflows/*.yml` is pinned to a commit SHA and
  updated weekly by Dependabot.
- The Unraid installer asks for the **bind address** of the web port
  (`ALC_INSTALL_BIND`, `0.0.0.0` by default) and publishes the port on that
  address only; `127.0.0.1` keeps it private. The compose example takes the
  same variable.
- The Unraid installer verifies the **image signature** with `cosign` before
  creating anything (`ALC_INSTALL_REQUIRE_SIGNATURE=1` fails when `cosign`
  is absent). It verifies and runs the digest it pulled for the requested
  repository, never the mutable tag or another repository's alias. It waits
  for the container to report `healthy` within a wall-clock limit (120 s by
  default, `ALC_INSTALL_START_TIMEOUT`), runs each `docker inspect` under a
  timeout, refuses a non-positive poll interval, never accepts an exited
  container, and records `start_verified` in the install manifest.

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

[Unreleased]: https://github.com/almali-dev/almali-library-curator/compare/v0.37.0...HEAD
[0.37.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.36.0...v0.37.0
[0.36.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.35.0...v0.36.0
[0.35.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.34.0...v0.35.0
[0.34.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.33.0...v0.34.0
[0.33.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.32.0...v0.33.0
[0.32.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.31.0...v0.32.0
[0.31.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.30.0...v0.31.0
[0.30.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.29.0...v0.30.0
[0.29.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.20.0...v0.21.0
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
