# Library Curator for Plex — Requirements

Nov 2, 2026 · @Miquel Àngel

## Intent

Library Curator for Plex shows what nobody watches in a Plex library, says why in one sentence per item, and removes only what the owner marks, after a grace period and a final re-check.

- **The problem it replaces.** In Maintainerr a rule is written in one place and its result is checked in another. The owner ends up not knowing whether a rule is right or what to do with its output.
- **The core idea.** No rule builder. A fixed set of categories, each with a few thresholds, and every result shown in place with its reason, size, disk and rating.
- **Who it is for.** First the owner's own Unraid server (3 Plex users). It is built from day one to be published in Unraid Community Applications, like Plex Smart Cache (PSC). Unlike PSC, it runs on any Docker host; the Unraid-specific parts (which disk holds a file) are optional.
- **Independence.** It does not depend on PSC, share its code or read its database. It reuses PSC's technology choices and visual design system only.
- **Reference.** The approved interactive mockup: [Library Curator mockup](https://claude.ai/artifact/9y9BoTrmDHjjndPyuL2zmt).

## Goals, non-goals, anti-goals

Version 1 is a manual curator: the app proposes, the owner marks, the app carries out the removal safely.

**Goals (v1)**

1. Detect the categories in the Detection section, per series, per season and per movie.
2. Show each candidate with its reason, size, disk and rating, in the same place where it is acted on.
3. Respect three kinds of protection: protected library, high rating, kept by hand.
4. Run the whole removal lifecycle: grace period, Leaving soon collection, final re-check, deletion, no re-download.
5. Ship as a Docker image with an installer and an Unraid Community Applications template.

**Non-goals (v1)**

- Automatic marking per category. The owner will design it later; v1 must not block it.
- A free-form rule builder.
- Freeing a target amount of space ("free 2 TB"). The owner's goal is knowing what is not watched, not a quota.
- Managing downloads, requests (Overseerr/Jellyseerr) or anything outside Plex, Sonarr and Radarr.
- Jellyfin or Emby support.

**Anti-goals (a failure even if it works)**

- Deleting anything the owner did not mark.
- Deleting something a user started watching during the grace period.
- Sonarr or Radarr downloading a removed item again.
- Reporting freed space that was not actually freed (for example a hardlinked file whose other link remains).
- A result the owner has to look up somewhere else to understand.

## Data sources

Plex is the only required source; every other connection adds precision or an action and the app must work, with reduced features, without it.

| Source | Required | Used for | Without it |
| --- | --- | --- | --- |
| Plex Media Server | Yes | Libraries, items, seasons, file paths, sizes, per-user play history, collections, Home hubs | App cannot run |
| Tautulli | No | Longer and more reliable per-user history | Plex history only |
| Sonarr | No (needed to delete series) | Series status (continuing, ended, cancelled), deletion, unmonitor, import exclusion | Series are shown but cannot be removed |
| Radarr | No (needed to delete movies) | Deletion, import exclusion | Movies are shown but cannot be removed |
| TMDB (API key) | No | Ratings and production status when Sonarr has none | Plex's own rating only |
| Unraid host filesystem | No | Which array disk or pool holds each file, hardlink count | Size shown without disk; freed space marked as estimated |

- **Users.** "A user" is a Plex account with access to the server, including managed Home users. The owner can exclude accounts (for example a guest) from all calculations.
- **IMDb.** IMDb has no free public API. v1 uses the rating Plex already stores (often IMDb or Rotten Tomatoes, depending on the agent) and TMDB as a fallback. Which rating to show is a setting.
- **Credentials.** Tokens and API keys are entered in the app, stored encrypted at rest, never logged and never returned in full by any API.

## Detection categories

Five fixed categories; each candidate carries the category, the unit it applies to (series, seasons or movie) and a one-sentence reason built from the facts that made it match.

| Category | Applies to | Matches when | Default threshold | Example reason |
| --- | --- | --- | --- | --- |
| Stalled series | The unopened seasons | Someone watched part of the show, then later seasons sat with no play | No play of those seasons for 6 months | Anna watched S1 in March 2025. S2 and S3 were never opened. |
| Never watched | Series or movie | No user has played any part since it was added | Added more than 3 months ago | Added April 2024. 0 episodes played by any of the 3 users. |
| Cancelled, no viewers | Whole series | Sonarr or TMDB says ended or cancelled, and no user is mid-show | Last play more than 6 months ago | Cancelled in 2022. Last play August 2023, nobody mid-season. |
| Seasons everyone finished | Finished seasons only | Every user who follows the show has watched those seasons, and later seasons are still pending | All followers finished them more than 1 month ago | Marc and Anna (the only followers) both finished S1. S2 is in progress. |
| Movies not rewatched | Movie | Watched at least once, nobody played it since | Last play more than 18 months ago | Watched once by Marc in February 2024. Nobody since. |

**Definitions**

- **Follower of a show:** a user who has played at least one episode of it. Users who never started it do not count towards "everyone".
- **Watched:** an episode or movie marked watched in Plex, or played past 90% in history.
- **Mid-show:** a follower who has watched some but not all aired episodes and played one within the stalled-series window.
- An item can match several categories; it appears once under All, with every matching reason.
- Thresholds are global settings in v1. Per-library thresholds are a later decision.
- A scan runs on a schedule (default daily) and on demand. Results show when the last scan ran.

## Protection

A protected item is never a candidate and never deleted; it is listed in the Protected view with what protects it and the category it would otherwise match.

| Protection | Set by | Covers | Example |
| --- | --- | --- | --- |
| Protected library | Settings, per Plex library | Everything in that library | Filmoteca: Blade Runner, 2001: A Space Odyssey, The Sting |
| High rating | Settings, one minimum rating (default 8.0) | Any item at or above it, seasons included | Severance, 8.7 |
| Kept forever | "Keep forever" button on any row | That item only | The Office (US) |

- Protection is checked at scan time and again at the final re-check before deletion.
- Removing a protection (unprotecting a library, lowering the threshold, undoing "Keep forever") only makes items candidates again; it never marks them.
- The rating rule can be switched off.

## Removal lifecycle

Nothing is deleted unless the owner marked it, the grace period has passed, and a final re-check confirms it still qualifies and nobody has started it.

&#91;embedded content: removal lifecycle · 1 decision, 2 outcomes\]

The final check is the only path to deletion; a failed check, an undo or "Keep it" sends the item back to the candidates.

1. **Candidate.** Found by a scan. No change in Plex, Sonarr or Radarr.
2. **Marked.** The owner presses "Mark for removal". The grace period starts (default 14 days, range 3 to 60). Undo is possible until deletion.
3. **Leaving soon.** The item is added to a Plex collection named "Leaving soon" (name configurable), promoted to every user's Home. The collection is created if missing and only ever holds marked items.
4. **Final re-check** on the last day. The item is kept, and leaves the collection, if any of these is true:
   - it no longer matches its category;
   - it became protected;
   - any user played any part of it after it was marked. The reason is shown in Leaving soon ("Removal cancelled: Marc started watching on 30 October").
5. **Deleted.** Through Sonarr (series or the exact seasons) or Radarr (movie), with files. In the same step the item is unmonitored and added to the import exclusion list so it is not downloaded again. The item leaves the collection.
6. **Recorded.** History keeps title, unit, size, disk, date, reason and who marked it. Freed space is the sum of files whose last link was removed; hardlinked files are counted only when every link is gone.

**Failure rules**

- If Sonarr or Radarr is unreachable or returns an error, nothing is deleted, the item stays in Leaving soon flagged "Removal failed" with the error, and it retries at the next scan.
- "Deleted" is shown only after the app confirms the files are gone from disk. A 2xx from Sonarr or Radarr alone is not proof.
- A dry-run mode does every step except steps 3 and 5 and reports what would happen.

## Screens

The main screen follows the approved [mockup](https://claude.ai/artifact/9y9BoTrmDHjjndPyuL2zmt): a summary row, three tabs, and every action on the row it applies to.

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

**Rules**

- Visual system: Plex Smart Cache's design system (dark HUD look, Tabler base, the same colour tokens, type and breakpoints), so both apps read as one family.
- Every badge has text and an icon; colour is never the only cue.
- Works from 320 px wide with no horizontal page scroll; touch targets at least 44 × 44 px.
- UI text in English, Catalan and Spanish from day one; no hard-coded strings in templates or scripts.

## Packaging and delivery

Shipped the same way as Plex Smart Cache: one Docker image on GHCR, an Unraid installer script, and an Unraid Community Applications (CA) template, plus a docker-compose example for any other Docker host.

| Deliverable | Requirement |
| --- | --- |
| Docker image | `ghcr.io/<owner>/library-curator-for-plex`, multi-arch (amd64, arm64), non-root with `PUID`/`PGID` (Unraid default 99/100), health check, signed release tags plus `latest` |
| Installer | `install-unraid.sh` modelled on PSC's v2 installer: asks for paths, port, user and password, generates the secret key, verifies its own download checksum, writes the container; `uninstall-unraid.sh` removes only what it created |
| CA template | `unraid-template.xml` with Repository, Registry, Support, Project, TemplateURL, Category, WebUI, and an **Icon that exists** (PSC's template has none, see PSC issue #850) |
| CA listing | A support thread on the Unraid forum and the template repository submitted to CA, as CA requires; to be confirmed against CA's current submission rules before release |
| Docs | README, Unraid deploy guide, CHANGELOG from the first release |

**Container mounts**

| Container path | Host default | Mode | Why |
| --- | --- | --- | --- |
| `/config` | `/mnt/user/appdata/library-curator-for-plex` | rw | Settings, database, logs, backups |
| `/media` | The media share | ro | File sizes and hardlink counts; deletion goes through Sonarr/Radarr, so no write access is needed |
| `/unraid/mnt` | `/mnt` | ro, optional | Which array disk or pool holds each file |

- Default web port: 17540 (one above PSC's 17539, so both run side by side). My call; changeable.
- Plex path mapping works as in PSC: a path map variable when Plex reports different container paths.
- Starts in dry-run on a fresh install; the owner turns real deletion on in Settings.

## Non-functional requirements

The app deletes media, so safety and verifiable outcomes outrank speed and features.

- **Technology (my call, reusing PSC's).** Python, Flask behind Gunicorn, SQLite, server-rendered pages with the same Tabler-based look. No shared code or database with PSC.
- **Safety.** Deletion only through the lifecycle above. A global kill switch in Settings stops all deletions at once. Every deletion is logged before and after it runs.
- **Verified outcomes.** A success message, a "Deleted" status or a freed-space figure comes only from a checked result, and each has a test for its failure branch.
- **Security.** Login required (user and password set at install), CSRF protection, secure session cookie, secrets encrypted at rest and never logged, outbound calls only to the configured services.
- **Load.** A scan must not slow Plex: it reads through the Plex API with paging and a rate limit, never by writing to Plex's database. Target: a 5,000-movie, 300-series library scanned in under 5 minutes. My call; adjust once measured.
- **i18n.** English, Catalan and Spanish; every string through translation keys.
- **Testing.** Test-first for every rule in the Detection, Protection and Lifecycle sections, through the public interface; fake Plex, Sonarr and Radarr servers in tests rather than mocks of the app's own code; CI on every pull request.
- **Logging.** Structured logs, no `print()`, no tokens or keys.

## Naming and open questions

The app is named Library Curator for Plex, and Plex Smart Cache becomes Smart Cache for Plex, following Plex's guideline of "\<Name> for Plex" instead of "Plex" inside the name.

- **What the guidelines say.** The [Plex trademark guidelines](https://www.plex.tv/about/privacy-legal/plex-trademarks-and-guidelines/) and the [developer guidance](https://www.plex.tv/?p=43) allow "\<Name> for Plex" when the name is unique, and prohibit Plex or derivatives in the app's own name, domains and trademark filings. Plex has enforced this before (the MK Plex add-on was withdrawn after a legal letter, [BetaNews](https://betanews.com/2017/09/01/mk-plex-kodi-addon-dies)).
- **Unraid stays out of both names.** Library Curator runs on any Docker host. Smart Cache for Plex is Unraid-only, which its description, CA category and README say in their first line.
- **Family prefix: undecided.** A shared word in front of both names (for example "Tower") would mark them as one family. The owner has not decided yet.
- **Before release.** Check both names for clashes on GitHub, Docker Hub and Unraid CA.
- **Disclaimer.** The README, the About page and the CA template state that the app is not affiliated with or endorsed by Plex Inc.

**Open questions**

- [ ] Whether to add a shared family prefix (for example "Tower") to both names.
- [ ] Which rating to show by default: Plex's stored rating, TMDB, or both.
- [ ] Per-library thresholds in v1, or global only (this document assumes global).
- [ ] Should a Plex user other than the owner be able to see the app or the Leaving soon reasons?
- [ ] Design of the automatic marking mode (after v1).
