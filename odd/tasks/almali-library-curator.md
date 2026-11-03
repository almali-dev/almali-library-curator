# Almali Library Curator for Plex — v1 (feature)

Feature doc for the ODD implementation of **Almali Library Curator for Plex**, version 1.
Source of truth for scope: `docs/Library-Curator-for-Plex.md` (the requirements file, Nov 2, 2026 · @Miquel Àngel).
This document quotes the requirements verbatim where a rule, message or example must not drift.

Status: T1–T4 done (v0.1.0–v0.4.0) · T5 next · T6–T15 not started.

## Specs

### S1 — Product intent and v1 boundaries

> Library Curator for Plex shows what nobody watches in a Plex library, says why in one sentence per item, and removes only what the owner marks, after a grace period and a final re-check.

> **The core idea.** No rule builder. A fixed set of categories, each with a few thresholds, and every result shown in place with its reason, size, disk and rating.

Goals (v1), verbatim:

1. Detect the categories in the Detection section, per series, per season and per movie.
2. Show each candidate with its reason, size, disk and rating, in the same place where it is acted on.
3. Respect three kinds of protection: protected library, high rating, kept by hand.
4. Run the whole removal lifecycle: grace period, Leaving soon collection, final re-check, deletion, no re-download.
5. Ship as a Docker image with an installer and an Unraid Community Applications template.

Non-goals (v1), verbatim:

- Automatic marking per category. The owner will design it later; v1 must not block it.
- A free-form rule builder.
- Freeing a target amount of space ("free 2 TB"). The owner's goal is knowing what is not watched, not a quota.
- Managing downloads, requests (Overseerr/Jellyseerr) or anything outside Plex, Sonarr and Radarr.
- Jellyfin or Emby support.

Anti-goals (a failure even if it works), verbatim:

- Deleting anything the owner did not mark.
- Deleting something a user started watching during the grace period.
- Sonarr or Radarr downloading a removed item again.
- Reporting freed space that was not actually freed (for example a hardlinked file whose other link remains).
- A result the owner has to look up somewhere else to understand.

### S2 — Data sources and degraded operation

> Plex is the only required source; every other connection adds precision or an action and the app must work, with reduced features, without it.

| Source | Required | Used for | Without it |
| --- | --- | --- | --- |
| Plex Media Server | Yes | Libraries, items, seasons, file paths, sizes, per-user play history, collections, Home hubs | App cannot run |
| Tautulli | No | Longer and more reliable per-user history | Plex history only |
| Sonarr | No (needed to delete series) | Series status (continuing, ended, cancelled), deletion, unmonitor, import exclusion | Series are shown but cannot be removed |
| Radarr | No (needed to delete movies) | Deletion, import exclusion | Movies are shown but cannot be removed |
| TMDB (API key) | No | Ratings and production status when Sonarr has none | Plex's own rating only |
| Unraid host filesystem | No | Which array disk or pool holds each file, hardlink count | Size shown without disk; freed space marked as estimated |

> - **Users.** "A user" is a Plex account with access to the server, including managed Home users. The owner can exclude accounts (for example a guest) from all calculations.
> - **IMDb.** IMDb has no free public API. v1 uses the rating Plex already stores (often IMDb or Rotten Tomatoes, depending on the agent) and TMDB as a fallback. Which rating to show is a setting.
> - **Credentials.** Tokens and API keys are entered in the app, stored encrypted at rest, never logged and never returned in full by any API.

### S3 — Detection categories

Five fixed categories; each candidate carries the category, the unit it applies to (series, seasons or movie) and a one-sentence reason built from the facts that made it match.

| Category | Applies to | Matches when | Default threshold | Example reason |
| --- | --- | --- | --- | --- |
| Stalled series | The unopened seasons | Someone watched part of the show, then later seasons sat with no play | No play of those seasons for 6 months | Anna watched S1 in March 2025. S2 and S3 were never opened. |
| Never watched | Series or movie | No user has played any part since it was added | Added more than 3 months ago | Added April 2024. 0 episodes played by any of the 3 users. |
| Cancelled, no viewers | Whole series | Sonarr or TMDB says ended or cancelled, and no user is mid-show | Last play more than 6 months ago | Cancelled in 2022. Last play August 2023, nobody mid-season. |
| Seasons everyone finished | Finished seasons only | Every user who follows the show has watched those seasons, and later seasons are still pending | All followers finished them more than 1 month ago | Marc and Anna (the only followers) both finished S1. S2 is in progress. |
| Movies not rewatched | Movie | Watched at least once, nobody played it since | Last play more than 18 months ago | Watched once by Marc in February 2024. Nobody since. |

Definitions, verbatim:

- **Follower of a show:** a user who has played at least one episode of it. Users who never started it do not count towards "everyone".
- **Watched:** an episode or movie marked watched in Plex, or played past 90% in history.
- **Mid-show:** a follower who has watched some but not all aired episodes and played one within the stalled-series window.
- An item can match several categories; it appears once under All, with every matching reason.
- Thresholds are global settings in v1. Per-library thresholds are a later decision.
- A scan runs on a schedule (default daily) and on demand. Results show when the last scan ran.

### S4 — Protection

> A protected item is never a candidate and never deleted; it is listed in the Protected view with what protects it and the category it would otherwise match.

| Protection | Set by | Covers | Example |
| --- | --- | --- | --- |
| Protected library | Settings, per Plex library | Everything in that library | Filmoteca: Blade Runner, 2001: A Space Odyssey, The Sting |
| High rating | Settings, one minimum rating (default 8.0) | Any item at or above it, seasons included | Severance, 8.7 |
| Kept forever | "Keep forever" button on any row | That item only | The Office (US) |

> - Protection is checked at scan time and again at the final re-check before deletion.
> - Removing a protection (unprotecting a library, lowering the threshold, undoing "Keep forever") only makes items candidates again; it never marks them.
> - The rating rule can be switched off.

### S5 — Removal lifecycle

> Nothing is deleted unless the owner marked it, the grace period has passed, and a final re-check confirms it still qualifies and nobody has started it.

> The final check is the only path to deletion; a failed check, an undo or "Keep it" sends the item back to the candidates.

1. **Candidate.** Found by a scan. No change in Plex, Sonarr or Radarr.
2. **Marked.** The owner presses "Mark for removal". The grace period starts (default 14 days, range 3 to 60). Undo is possible until deletion.
3. **Leaving soon.** The item is added to a Plex collection named "Leaving soon" (name configurable), promoted to every user's Home. The collection is created if missing and only ever holds marked items.
4. **Final re-check** on the last day. The item is kept, and leaves the collection, if any of these is true:
   - it no longer matches its category;
   - it became protected;
   - any user played any part of it after it was marked. The reason is shown in Leaving soon ("Removal cancelled: Marc started watching on 30 October").
5. **Deleted.** Through Sonarr (series or the exact seasons) or Radarr (movie), with files. In the same step the item is unmonitored and added to the import exclusion list so it is not downloaded again. The item leaves the collection.
6. **Recorded.** History keeps title, unit, size, disk, date, reason and who marked it. Freed space is the sum of files whose last link was removed; hardlinked files are counted only when every link is gone.

### S6 — Failure rules, dry run and kill switch

> - If Sonarr or Radarr is unreachable or returns an error, nothing is deleted, the item stays in Leaving soon flagged "Removal failed" with the error, and it retries at the next scan.
> - "Deleted" is shown only after the app confirms the files are gone from disk. A 2xx from Sonarr or Radarr alone is not proof.
> - A dry-run mode does every step except steps 3 and 5 and reports what would happen.

> - **Safety.** Deletion only through the lifecycle above. A global kill switch in Settings stops all deletions at once. Every deletion is logged before and after it runs.
> - **Verified outcomes.** A success message, a "Deleted" status or a freed-space figure comes only from a checked result, and each has a test for its failure branch.

### S7 — Screens and UI rules

The main screen follows the approved mockup, now available locally as `docs/Plex Smart Curator.pdf` (one page, 1080 × 885 pt). Layout and copy taken from it, verbatim:

- Left sidebar: the product name, then the nav entries "LIBRARY HEALTH", "HISTORY", "CONNECTIONS", "SETTINGS"; below it a "// SOURCES" panel listing the live connections ("Plex · 3 users", "Tautulli · history since 2021", "Sonarr · Radarr", "Unraid · 6 array disks").
- Header: the screen title, the line "What nobody watches, why it is here, and what happens next. Nothing is removed until you mark it.", and on the right "Last scan · today 06:00".
- Summary row of four cards, labels prefixed with "//": "// CANDIDATES" "433 GB" "7 items you could remove"; "// LEAVING SOON" "60 GB" "2 in grace · 14-day period"; "// PROTECTED" "385 GB" "5 items · Filmoteca, rating, by hand"; "// FREED SO FAR" "1.31 TB" "38 items since July · disk3 now 71 %".
- Tabs: "CANDIDATES · 7", "LEAVING SOON · 3", "PROTECTED · 5", with the count in the tab.
- Category filters as buttons with counts: "All 7", "Stalled series 1", "Never watched 3", "Cancelled, no viewers 1", "Seasons everyone finished 1", "Movies not rewatched 1".
- Helper line above the rows: "Everything that matches at least one category, largest first."
- Each row: a type badge ("SERIES", "SEASONS", "MOVIE"), the title with its unit ("Lost in Space S2–S3", "Foundation S1", "Tenet 2020"), the one-sentence reason under it, then size ("142 GB"), disk ("disk3"), the rating with a star and its source ("IMDb"), and the two row actions "KEEP FOREVER" and "MARK FOR REMOVAL".

The mockup's own title reads "SMART CURATOR for Plex · Unraid"; the app shows "LIBRARY CURATOR for Plex" instead, because S10 keeps Unraid out of the product name.

The main screen follows the approved mockup: a summary row, three tabs, and every action on the row it applies to.

| Screen | What it shows | Actions |
| --- | --- | --- |
| Library health · summary | Space in candidates, in grace, protected, freed so far; time of last scan | Run scan now |
| Library health · Candidates | Category buttons with counts and a one-line explanation; rows sorted by size with type, title, unit, reason, size, disk, rating | Mark for removal / Undo, Keep forever |
| Library health · Leaving soon | Marked items, days left as a bar, re-check status, cancelled removals with their reason | Keep it (cancels the removal) |
| Library health · Protected | Protected items with what protects them | Undo "Keep forever" |
| History | Every deletion and cancellation, newest first, freed space per month | Filter, export CSV |
| Connections | Status and test of Plex, Tautulli, Sonarr, Radarr, TMDB, host paths | Edit, test |
| Settings | Thresholds, grace days, protected libraries, minimum rating, excluded users, collection name, scan schedule, dry run | Save |
| First-run wizard | Plex sign-in, library selection, optional connections, protected libraries | Step through |

Rules, verbatim:

> - Visual system: Plex Smart Cache's design system (dark HUD look, Tabler base, the same colour tokens, type and breakpoints), so both apps read as one family.
> - Every badge has text and an icon; colour is never the only cue.
> - Works from 320 px wide with no horizontal page scroll; touch targets at least 44 × 44 px.
> - UI text in English, Catalan and Spanish from day one; no hard-coded strings in templates or scripts.

### S11 — Library reuse (the stack Plex Smart Cache already proves)

The owner asks for the libraries Plex Smart Cache already uses, so both apps run on a proven set instead of a new one. Reuse means the same library, the same major version and the same purpose — never shared code, a shared database or a runtime dependency between the two apps.

| Library | PSC role | Role here |
| --- | --- | --- |
| Flask 3.1 | web framework | web framework |
| Flask-WTF 1.2 | form and CSRF handling | CSRF protection on every mutating route (S9) |
| Gunicorn 25 | WSGI server | WSGI server |
| requests 2.33 | outbound HTTP | Plex, Sonarr, Radarr, Tautulli and TMDB clients |
| pydantic 2.12 | payload validation | validation of every external API payload |
| pydantic-settings 2.13 | settings validation | validation of the settings file and the `LCP_` environment overrides |
| psutil 7.2 | host inspection | which array disk or pool holds each file (S2) |
| Tabler 1.0.0-beta20 (vendored CSS) | dashboard design system | the same vendored stylesheet, copied into this app in T9, with its LICENSE, plus this app's own HUD layer on top of the tokens documented in PSC's `design/system.md` |

Deliberately not reused: `aiohttp` and `flask-socketio` (the app is synchronous server-rendered in v1), `flask-limiter` (the rate limit the requirements ask for is on outbound Plex reads, not on inbound routes), `pyyaml` (settings are JSON), `eventlet` (the default WSGI worker stays plain Gunicorn).

Every one of these libraries is already installed in the development environment, so the test suite runs without a package install step.

### S8 — Packaging and delivery

> Shipped the same way as Plex Smart Cache: one Docker image on GHCR, an Unraid installer script, and an Unraid Community Applications (CA) template, plus a docker-compose example for any other Docker host.

| Deliverable | Requirement |
| --- | --- |
| Docker image | `ghcr.io/<owner>/library-curator-for-plex`, multi-arch (amd64, arm64), non-root with `PUID`/`PGID` (Unraid default 99/100), health check, signed release tags plus `latest` |
| Installer | `install-unraid.sh` modelled on PSC's v2 installer: asks for paths, port, user and password, generates the secret key, verifies its own download checksum, writes the container; `uninstall-unraid.sh` removes only what it created |
| CA template | `unraid-template.xml` with Repository, Registry, Support, Project, TemplateURL, Category, WebUI, and an **Icon that exists** (PSC's template has none, see PSC issue #850) |
| CA listing | A support thread on the Unraid forum and the template repository submitted to CA, as CA requires; to be confirmed against CA's current submission rules before release |
| Docs | README, Unraid deploy guide, CHANGELOG from the first release |

Container mounts, verbatim:

| Container path | Host default | Mode | Why |
| --- | --- | --- | --- |
| `/config` | `/mnt/user/appdata/library-curator-for-plex` | rw | Settings, database, logs, backups |
| `/media` | The media share | ro | File sizes and hardlink counts; deletion goes through Sonarr/Radarr, so no write access is needed |
| `/unraid/mnt` | `/mnt` | ro, optional | Which array disk or pool holds each file |

> - Default web port: 17540 (one above PSC's 17539, so both run side by side). My call; changeable.
> - Plex path mapping works as in PSC: a path map variable when Plex reports different container paths.
> - Starts in dry-run on a fresh install; the owner turns real deletion on in Settings.

### S9 — Non-functional requirements

> The app deletes media, so safety and verifiable outcomes outrank speed and features.

- **Technology (my call, reusing PSC's).** Python, Flask behind Gunicorn, SQLite, server-rendered pages with the same Tabler-based look. No shared code or database with PSC.
- **Security.** Login required (user and password set at install), CSRF protection, secure session cookie, secrets encrypted at rest and never logged, outbound calls only to the configured services.
- **Load.** A scan must not slow Plex: it reads through the Plex API with paging and a rate limit, never by writing to Plex's database. Target: a 5,000-movie, 300-series library scanned in under 5 minutes. My call; adjust once measured.
- **i18n.** English, Catalan and Spanish; every string through translation keys.
- **Testing.** Test-first for every rule in the Detection, Protection and Lifecycle sections, through the public interface; fake Plex, Sonarr and Radarr servers in tests rather than mocks of the app's own code; CI on every pull request.
- **Logging.** Structured logs, no `print()`, no tokens or keys.

### S10 — Naming and release constraints

- The app is named Library Curator for Plex, and Plex Smart Cache becomes Smart Cache for Plex, following Plex's guideline of "\<Name> for Plex" instead of "Plex" inside the name.
- Unraid stays out of both names. Library Curator runs on any Docker host.
- The README, the About page and the CA template state that the app is not affiliated with or endorsed by Plex Inc.
- Before release, check both names for clashes on GitHub, Docker Hub and Unraid CA.

Open questions recorded in the requirements (unchanged status unless stated in `## Log`):

- [ ] Whether to add a shared family prefix (for example "Tower") to both names.
- [ ] Which rating to show by default: Plex's stored rating, TMDB, or both.
- [ ] Per-library thresholds in v1, or global only (this document assumes global).
- [ ] Should a Plex user other than the owner be able to see the app or the Leaving soon reasons?
- [ ] Design of the automatic marking mode (after v1).

## Tasks

Routes: `inline` = parent session; `worker` = bounded `gentle-ai-worker` with an explicit allowed-edit-surface list.
Every task starts with a RED test through the public interface, and ends with the focused test plus the full suite green.

| ID | Task | Specs | Route | Commit | Risk |
| --- | --- | --- | --- | --- | --- |
| T1 | Project foundation: layout, settings, SQLite schema + migrations, structured logging, i18n scaffolding, health endpoint, pytest setup, CI on every pull request | S9 | inline | W1 `feat:` — 4 slices: 61a29b0, 0d798b4, 20c0908, 0e58c17 | item 3 (config format and public routes are contracts) |
| T2 | Plex client and sync: HTTP client with paging, rate limit, retries and redirect refusal; models; sync of libraries, items, seasons, episodes, users, play history | S2, S3 (data), S9, S11 | inline after worker admission failures | W2: 809e3c1, bdd4ed5, bdc0bfa (redirect fix) | item 2 (credentials and outbound service boundary) — verified inline; see L22 |
| T3 | Optional connections: Sonarr and Radarr (status, delete, unmonitor, import exclusion), Tautulli (longer history), TMDB (rating, production status), host filesystem (disk, hardlink count) — each degrading cleanly when absent | S2 | 3 parallel workers + parent integration | W3: d3dc2ca, 8ab13f1, c22dbb1, 10fa59e · **v0.3.0** | item 3 (external API contracts) — same L22 fidelity caveat, live check at T15 |
| T4 | Detection engine: 5 categories, global thresholds, follower/watched/mid-show definitions, one-sentence reasons, multi-category match shown once | S3 | worker | W4: 943cb22 · **v0.4.0** | item 6 (detection rules are the product) — 29 rule tests over sync fixtures |
| T5 | Protection engine: protected library, high rating (default 8.0), kept forever; checked at scan time and at re-check; unprotected items only become candidates again | S4 | worker | W5 `feat: protection engine` | item 6 |
| T6 | Lifecycle state machine: candidate → marked → Leaving soon collection → grace (default 14, range 3–60) → final re-check → deleted → recorded; undo, "Keep it", cancellation reason "Removal cancelled: Marc started watching on 30 October" | S5 | worker | W6 `feat: removal lifecycle` | item 1 (deletion state) |
| T7 | Deletion execution: Sonarr/Radarr delete with files, unmonitor, import exclusion, confirm files gone from disk before showing "Deleted", hardlink-aware freed space, history record, failure rules with retry | S5, S6 | worker | W7 `feat: verified deletion and history` | item 1 (irreversible deletion) |
| T8 | Scan orchestration: daily schedule + on demand, last-scan timestamp, dry-run mode (skips collection add and deletion, reports what would happen), global kill switch | S3, S6 | inline | W8 `feat: scan scheduler, dry run and kill switch` | item 1 (kill switch and dry run) |
| T9 | Web UI: vendored Tabler 1.0.0-beta20 plus this app's own HUD layer (tokens from PSC's `design/system.md`), reproducing the layout and copy of `docs/Plex Smart Curator.pdf`: sidebar, "// SOURCES" panel, header, four summary cards, three tabs, category filters, candidate rows, History, Connections, Settings, first-run wizard | S7, S11 | worker | W9 `feat: web ui screens` | item 3 (public screens and copy) |
| T10 | i18n completion (en/ca/es, no hard-coded strings) plus responsive 320 px, 44×44 px touch targets, badge text + icon, no colour-only cue | S7, S9 | inline | W10 `feat: i18n and accessibility pass` | item 3 |
| T11 | Security: install-time login, CSRF on every mutating route (Flask-WTF), secure session cookie, secrets encrypted at rest and never returned in full, log redaction, outbound calls only to configured services | S2, S9, S11 | worker | W11 `feat: security hardening` | item 2 (auth, secrets, guards) |
| T12 | Packaging: multi-arch Dockerfile (amd64, arm64), non-root with PUID/PGID, health check, GHCR publish with signed tags plus `latest`, docker-compose example | S8 | inline | W12 `build: docker image and compose example` | item 5 (delivery) |
| T13 | Unraid delivery: `install-unraid.sh` modelled on PSC v2, `uninstall-unraid.sh`, `unraid-template.xml` with an icon that exists, deploy guide, port 17540 default, /config /media /unraid/mnt mounts | S8 | worker | W13 `feat: unraid installer and ca template` | item 5 (delivery) |
| T14 | Docs: README, CHANGELOG from the first release, About page and CA template disclaiming Plex affiliation, naming checks before release | S10 | inline | W14 `docs: readme, changelog and release notes` | item 3 |
| T15 | Release verification: full suite green, scan of 5,000 movies / 300 series under 5 minutes, CA submission checklist, first release cut | S8, S9 | inline | W15 `chore: release verification` | item 5 |

Work-unit commit budget: about 400 authored changed lines per commit; a task whose Risk names a high-risk item (T6, T7, T8, T11, T12, T13) gets its own commit so native review can take it alone.

## Log

- L1 — User's original request (verbatim): "Tens el fitxer de requeriments d'una nova aplicacio. Fes un explore dels requeriments i comencem amb odd"
- L2 — Requirements source: `docs/Library-Curator-for-Plex.md`, dated "Nov 2, 2026 · @Miquel Àngel". Treated as the authoritative scope for this feature.
- L3 — Exploration (2026-11-02): the project directory held only `docs/Library-Curator-for-Plex.md` and `.atl/skill-registry.md`. No git repository, no code, no tests. Python 3.14.3, Docker 29.1.3 and git 2.43.0 are available.
- L4 — Exploration: the reference implementation is the sibling checkout `/home/mac/work/plex_smart_cache` (Flask + Gunicorn + SQLite + Tabler), which holds `design/system.md` (the HUD token source of truth), `install-unraid.sh`, `install-unraid-v2.sh`, `uninstall-unraid.sh` and `unraid-template.xml`. Design tokens and installer behaviour are copied, never shared code or database.
- L5 — Exploration gap G1: the approved mockup `https://claude.ai/artifact/9y9BoTrmDHjjndPyuL2zmt` is not retrievable from this environment (JavaScript-rendered, session-authenticated). The screen layout in S7 therefore comes from the requirements text plus the PSC design system, not from the mockup pixels.
- L6 — Exploration gap G2: the requirements file carries an unresolved embed placeholder where the lifecycle diagram was: "[embedded content: removal lifecycle · 1 decision, 2 outcomes]". The six numbered steps and the failure rules in S5/S6 fully specify the behaviour, so the diagram is a documentation-fidelity gap, not an implementation blocker.
- L7 — Exploration gap G3: the working directory is named `libray-curator-for-plex` (missing "b"), while the product name and the image path in S8 are `library-curator-for-plex`. The directory slug is a packaging-relevant decision for the user; it does not block T1–T11.
- L8 — Exploration finding G4: the requirements ask for "no `print()`", structured logs, a health check, and a Python/Flask stack; PSC runs Python 3.13 in its pinned requirements while this host has Python 3.14.3, so the new project pins its own Python version in the Dockerfile rather than copying PSC's pin.
- L9 — Decisions taken from the requirements text (not open): thresholds are global in v1; the default rating is the one Plex stores with TMDB as fallback and a setting to choose; automatic marking is out of scope for v1 but the data model must not block it.
- L10 — Decisions still open for the user (not blocking T1–T11): family prefix for both product names; default rating source; whether the repository slug is renamed; the GHCR owner segment in `ghcr.io/<owner>/library-curator-for-plex`.
- L11 — T1 delivered as four reviewable slices on `feature/library-curator-for-plex-v1`, each verified green on its own: 61a29b0 (package skeleton, settings, structured logging, en/ca/es catalogs), 0d798b4 (SQLite storage with forward-only migrations), 20c0908 (health endpoint and first page), 0e58c17 (CI on every pull request). Full suite: 13 tests green.
- L12 — T1 smoke check: gunicorn serving `library_curator.wsgi:application` answers `GET /healthz` with `{"status":"ok","version":"0.1.0","schema_version":1}`, `GET /?lang=ca` renders the Catalan strings, and the config directory is created with `settings.json` plus the SQLite database. The first slice boundary was moved because the initial single commit carried 961 authored lines; slices now stay near 400.
- L13 — Environment discovery: TCP port 17599 on this host is already taken by another application, so the default web port 17540 (S8) must be checked against the host before the image is published.
- L14 — The approved mockup reached the repository as `docs/Plex Smart Curator.pdf` (one page, 1080 x 885 pt). Gap G1 is closed: S7 now carries the mockup's layout and copy verbatim. The mockup's own title is "SMART CURATOR for Plex · Unraid"; the app title follows S10 and drops the Unraid suffix.
- L15 — The repository directory was renamed from `libray-curator-for-plex` to `library-curator-for-plex` (gap G3 closed). The product slug, the image path and the docs now agree with the product name. The old path is a compatibility symlink to the canonical checkout, not a second repository. The live session still retains its pre-rename Git identity.
- L16 — Owner decision: reuse the libraries Plex Smart Cache already proves. Recorded as S11 with the exact library set and the deliberate exclusions (aiohttp, flask-socketio, flask-limiter, pyyaml, eventlet). `pyproject.toml` now depends on Flask, Flask-WTF, Gunicorn, requests, pydantic, pydantic-settings and psutil, all already installed in the development environment. The Tabler 1.0.0-beta20 stylesheet is copied into this app during T9.

- L17 — User correction (verbatim): "He copiat a docs un pdf amb el mockup. Estaria be que fecis servir llibreries que ja estigui a PSC, si es pot, clar. Canvia el nom del directori, ja que efectivament, esta malament."
- L18 — User continuation (verbatim): "continua"
- L19 — Reconciled T2 commits: 809e3c1 (client, models and fake server tests), bdd4ed5 (sync, additive settings and migration 002). Implemented inline after worker admission failures; no worker completion exists. Existing suite observed: `python3 -m pytest -q`, exit 0, 23 tests green. This is self-verification, not proof of complete S2/S3 coverage.
- L20 — T2 assessment over committed range af68aae..bdd4ed5: nativeRisk medium, escalated item 2 (credentials and outbound configured-service boundary), independentVerifier true, reviewDue false (under_budget). No native review transaction started.
- L21 — Required verifier task mvzscezl-2-m0ly failed before inspection: "could not register launched worktree: Session Git identity changed; cannot register a worktree." No verdict or independent proof was produced. Do not retry or bypass the authority error in this live session. T2 is blocked, T3–T15 remain pending. Open a fresh session at the canonical checkout `/home/mac/work/library-curator-for-plex`, reconcile this file and Engram, then verify T2 before continuing; do not assume existing fake-server tests establish real Plex payload fidelity or credential safety.
- L22 — Fresh session at the canonical checkout (2026-11-02): verifier admission refused ("this change declares no named high-risk item" — no pending risk-declaring change in the fresh session; L20's assess remains the recorded authority: nativeRisk medium, reviewDue false, under budget). Prescribed inline fallback executed: full suite green before and after (23 → 24 tests), plus a credential-boundary read of af68aae..bdd4ed5. Findings: token never persisted (config.py pops `plex_token` from `_serializable`), never logged, masked in `__repr__`, sent only as a header — clean; ONE real leak: `requests` follows redirects and re-sends `X-Plex-Token` cross-host. Fixed test-first (RED on the fake server proved the redirected host was contacted): `allow_redirects=False` plus a non-retryable `PlexError` on 3xx, commit bdc0bfa. Remaining caveat carried forward, not blocking: fake-server payload fidelity against a real Plex is exercised only at T15 release verification, when a live server is available.
- L23 — T2 closed as verified inline with the L22 caveat. Slice-budget note: branch total since main is ~4,492 authored lines over 10 commits (T1+T2 work units); each work-unit commit stays near the ~400-line budget, recorded per task row.
- L24 — User instruction (verbatim): "Recorda de crear el repo a gh". Created https://github.com/mmarkimark/library-curator-for-plex (private; public visibility and CA/GHCR listing stay release decisions under S8/S10/T12/T15). `main` (docs) and `feature/library-curator-for-plex-v1` pushed; remote `origin` set. This resolves the L10 GHCR owner segment: `ghcr.io/mmarkimark/library-curator-for-plex`, to be confirmed at T12. CI note: `ci.yml` lives on the feature branch and triggers on pushes to main and on pull requests, so the first CI run happens when the feature PR opens — no run was missed.
- L25 — T3 in flight as three parallel bounded writers with disjoint allowed edit surfaces: Sonarr+Radarr clients (task mvztjoku-1-m8oq), Tautulli+TMDB clients (mvztjokz-2-qeho), host-filesystem adapter (mvztjol1-3-mkdt). Parent owns the config.py additive-settings integration (10 fields, env overrides, SECRET_FIELDS exclusion from settings.json) to apply after they return, avoiding mid-run suite races.
- L26 — User instruction (verbatim): "La idea és d'anar creant versions fins arribar a la 1.0 que serà la publicable. Crea un roadmap de versionat. La idea es que es vagi etiquetant segons el que anem fent. Tu has de tenir el control de les versions. Proposa la numeració com hagi de ser mentre anem avançant." Adopted policy in docs/ROADMAP.md: pre-1.0 every completed ODD task bumps 0.MINOR; PATCH for fixes inside a milestone; v1.0.0 = T15 and the only publishable tag. The orchestrator owns versions, bumps the two constants (library_curator/__init__.py, pyproject.toml) in the tag's chore commit, and pushes annotated tags at task close. Retroactive tags: v0.1.0 at 0e58c17 (T1), v0.2.0 at the chore commit (T2). Task rows record their tag in the Commit column from T3 on.
- L27 — T3 delivered by three parallel bounded writers with disjoint surfaces, then parent integration: d3dc2ca (Sonarr+Radarr, 28 tests), 8ab13f1 (Tautulli+TMDB, 19 tests; found and fixed a real LogRecord.message collision bug in the process), c22dbb1 (hostfs, 24 tests over real tmp_path Unraid trees with real hardlinks), 10fa59e (config wiring: 10 additive settings, LCP_* env overrides, SECRET_FIELDS frozenset replacing the plex_token-only exclusion — every api key and the tmdb token stay out of settings.json). Suite: 98 tests green. One worker test was reworked by the parent after wiring: the hostfs "settings do not carry the paths yet" degradation test became the explicit-unconfigured variant plus a behavior-level settings-reach test, because the fields now exist. Credential discipline spot-checked in every client: header-only keys (X-Api-Key, Bearer) or POST-body apikey for Tautulli, allow_redirects=False with non-retryable 3xx refusal everywhere, masked __repr__, secrets absent from caplog and exception strings — all asserted by fake-server tests. Tagged v0.3.0. Deviations accepted: service-prefixed exception names (T7 import clarity), raw-resource PUT for unmonitor (extra="ignore" models would drop fields), no paging on arr v3 collection endpoints, SonarrSeries.seasons statistics (additive, for T4/T7). Writer admission requires the exact first line `Spec: odd/tasks/<feature>.md, T<n>`.
- L28 — T4 incident and close: the worker (mvzudc2k-4-s6cg) failed on an opaque provider error ("assistant reported an error") after 43 turns, before returning its report — but its full deliverable was already in the tree and the whole suite passed (127 = 98 + 29 detection tests). Per the incident rule the parent verified the work directly instead of relaunching: every brief item is covered by a named test (example reason per category in en plus a parametrized ca/es pass, boundary exactly-at vs one-day-past for stalled and never-watched, 90 %/40 % watched triangulation, excluded users as non-followers and non-watchers, enricher degradation, mid-show cancellation, multi-category grouped once, rating fallback), and the parent read the five category implementations against the S3 table — faithful, including strict "more than N months" semantics with month-end clamping. Accepted interpretation recorded: stalled series counts any never-opened season after the earliest played one (not necessarily a contiguous prefix); cancelled-no-viewers falls back to added_at with an honest "never played" reason when there is no play at all. Enricher protocols defined in library_curator/detection/enrichers.py for T5/T6/T8 to implement. Committed as 943cb22, tagged v0.4.0.
