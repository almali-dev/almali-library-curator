# Pre-1.0 audit of v0.32.0

Date: 2026-11-12. Scope: the whole repository at v0.32.0 on `feature/almali-library-curator-v1`.

Method: seven independent read-only reviews (security, core pipeline, installation and release,
performance, UI/UX/i18n, documentation and public-release hygiene, architecture and tests).
Findings were read in code unless marked otherwise. Performance figures were measured on a
synthetic database built on the real schema (20,000 movies, 200,000 episodes, 500,000 play
events, 127 MB) on a fast development machine. Nothing was run against a real server.

## Verdict

The base is sound: fail-closed authentication, global CSRF, encrypted secrets, a deletion path
that only acts on items re-checked in the same run, dry run by default, and a suite of 1,516
passing tests with no mocks. It is not ready to be public. The largest gap is that the app has
never run against a real Plex, Radarr or Sonarr: every deletion path is proven only against
fakes, and three findings suggest the fakes differ from the real services.

## Blockers for 1.0

| Issue | Finding | Status |
|---|---|---|
| #37 | Plex file paths and sizes are read from a top-level `Part`, not `Media[].Part[]` | Unverified against a live server |
| #38 | Deletion through Radarr/Sonarr does not verify that the target owns the marked files | Read in code |
| #39 | A request is recorded as deleted when the files were never visible | Read in code; two tests pin it |
| #40 | Schema migrations are not atomic and a newer schema opens silently | Reproduced |
| #41 | `main` lacks everything since v0.16.0; releases are published without CI | Confirmed |
| #42 | The image package is private; no document shows a pull-and-run install | Confirmed |
| #43 | The Unraid template documents a path-map syntax the app ignores | Confirmed |
| #44 | Internal task logs and personal identity would be published | Confirmed |
| #45 | A running or failed scan is invisible | Read in code |
| #46 | The first-run wizard dead-ends at Libraries | Read in code |
| #47 | Reason sentences are stored in the scan-time language | Read in code |
| #48 | The Library page is unpaginated (2.3 s, 19.4 MB for 10,106 candidates) | Measured |

Two further blockers are not issues yet (see "Not filed"): the field test on a real server
(roadmap task T17), and screenshots plus a "why trust this with deletion" section.

## High severity

| Issue | Finding |
|---|---|
| #49 | A failed Tautulli import or arr linking does not block deletion |
| #50 | Items Plex no longer has are never pruned |
| #51 | Pagination stops after one page when `totalSize` is absent (unverified) |
| #52 | The Radarr import exclusion is posted to the Sonarr endpoint (unverified) |
| #53 | A failed `docker run` leaves an install neither script can recover |
| #54 | The documented upgrade command drops labels, mounts and signature verification |
| #55 | Publishing has no concurrency control, tag immutability or stable-only `latest` |
| #56 | The installer checksum is circular and a missing cosign only warns |
| #57 | `scan_runs.report` grows about 1.9 GB a year and is re-parsed on every page (measured) |
| #58 | Every scan stats every media file and wakes all array disks (read in code) |
| #59 | A live scan holds the SQLite write lock for the whole Plex sync (measured) |
| #60 | The Plex sync materialises the whole library in memory (about 650 MB estimated) |
| #63 | Every row action resets the tab, filter and scroll position |
| #64 | The destructive path has no visual hierarchy, deletion date or go-live confirmation |
| #65 | On phones the candidate list sits below several screens of chrome |
| #66 | Several documented claims do not match the code |
| #67 | CI silently skips workflow and layout tests and has no lint or type gate |
| #68 | No migration-chain test and no HTTP end-to-end deletion cycle |

## Medium and low severity

| Issue | Finding |
|---|---|
| #61 | A changed service address receives the stored credential |
| #62 | No security headers; default session cookie name |
| #69 | Cancelling after a failed removal leaves the title unmonitored and excluded |
| #70 | A newly added season is immediately a stalled candidate |
| #71 | Container run options, password handling and proxy lockout are not hardened |
| #72 | Shared page title, unlabelled checkboxes, small type, weak focus ring |
| #73 | History dates and CSV headers not localised; language links return 405 after a POST |
| #74 | Scans re-import all history; TMDB cached for one scan; static assets and SQLite tuning |

Existing issues: #15 (dry-run copy: 124 ms and +131 MB RSS measured; proposed fix is a
temp-file backup under the config directory, after #57) and #36 (proposed fix: drop the
pre-sync invalidation and record the disks inside the sync transaction, plus an index on
`play_events(source, played_at)`).

## Not filed

These are additions, not defects. The repository's feature form requires the reporter's own
affirmations, so they wait for the owner.

- Field test on a real server (T17): two to four weeks of dry run, then one live deletion
  through each of Sonarr, Radarr and the Plex fallback.
- Screenshots and a "why trust this with deletion" section; privacy statement; FAQ,
  troubleshooting and a comparison with similar tools; architecture overview replacing links
  to internal task files; supported-versions table.
- Code of conduct and a support channel.
- Check active sessions before deleting.
- A richer audit trail: files, backend, arr side effects, mark and kill-switch events.
- Bulk selection, search and sort on the candidate list.
- More protection sources (watchlist, collection, label, in progress), or document their absence.
- Smaller refactors: an application-service layer for the mark use case, a shared HTTP client
  base for Radarr/Sonarr, splitting `create_app`, `load_settings` and `register_plex_actions`,
  the `detection`/`protection` import cycle, contributor tooling (task runner, pre-commit).
- Remaining checks before launch: name clashes on registries and Community Applications,
  brand assets free of third-party marks, private vulnerability reporting enabled.

## Done well

- Authentication fails closed (503 when unconfigured), constant-time comparisons, absolute
  12 h session lifetime, global sign-out.
- CSRF on every POST; destructive toggles go through a confirmation page.
- Secrets in an encrypted store with 0600 files; a log filter masks sensitive fields; only
  exception class names reach the database and the UI.
- Outbound calls never follow redirects, always time out and verify TLS.
- The app never unlinks media: deletion goes through Radarr, Sonarr or Plex, with media
  mounted read-only.
- Deletion acts only on ids that passed the final check in the same run, with an atomic claim
  and the kill switch read inside the transaction; a season scope never widens to the series.
- Set-based detection: 5.6 s for the whole synthetic library, under 2 ms per item.
- Hash-locked image dependencies, SHA-pinned actions, keyless image signing.
- No external requests from the UI; works without JavaScript; three languages with exact key
  parity; every status pairs an icon with text.
- Tests use real fake HTTP servers and injected clocks, with no mocks. The suite takes about
  11 minutes serially and about 2 minutes with `-n 6`.

## Strategic options

| Option | Effort | When |
|---|---|---|
| Publish from a fresh root with a noreply author; keep the private repository as archive | S | Before going public |
| Install from signed release assets instead of branch URLs | S | Release candidate |
| Decouple the redesign (T18) from 1.0 | n/a | Move to 1.1 |
| Demo mode with a synthetic library (reuses the test fakes) | S | Before 1.0 |
| Headless CLI (`scan --dry-run`, `status`) | S | Before 1.0 if cheap |
| Notifications (ntfy, Discord, Apprise) | S-M | 1.1 |
| Application-service layer, then a public REST API | M | After 1.0 |
| Archive / cold-tier mode instead of delete | L | After 1.0; highest safety risk |
| Media-server port for Jellyfin/Emby | L | After 1.0; needs a schema rename |

## Milestones

| Milestone | Content |
|---|---|
| 0.33 Public-ready | Deletion-safety guards, migration safety, release pipeline on `main`, hygiene |
| 0.34 Field test | First real-server run; contract fixes for Plex, Radarr and Sonarr |
| 1.0.0-rc | Repository and image public; onboarding, UX and docs blockers; external testers |
| 1.0.0 | Two weeks on a release candidate with no deletion-safety bug; first GitHub Release |
| 1.1 | Redesign, incremental scans, notifications, #15, #36 |

Proposed stability commitments from 1.0: `ALC_*` names, `settings.json` keys and `/config`
file names stable within 1.x; forward migrations automatic and downgrade refused safely; only
`/healthz` and the History CSV columns are public HTTP contracts; dry run by default and
"nothing deleted unless marked" change only with a major version.
