# Almali Library Curator for Plex — Requirements

Requirements, first written on Nov 2, 2026.

> **Naming note (Nov 3, 2026).** The product was renamed to **Almali Library Curator for Plex** and published under the `almali-dev` organization as `almali-dev/almali-library-curator`. The "Naming and open questions" section below carries that decision; the rest of this document keeps the scope as it was written on Nov 2, 2026.

## Intent

Almali Library Curator for Plex shows what nobody watches in a Plex library, says why in one sentence per item, and removes only what the owner marks, after a grace period and a final re-check.

- **The problem it replaces.** In Maintainerr a rule is written in one place and its result is checked in another. The owner ends up not knowing whether a rule is right or what to do with its output.
- **The core idea.** No rule builder. A fixed set of categories, each with a few thresholds, and every result shown in place with its reason, what the scope deletes, size, disk (when host paths are configured) and rating with its source.
- **Who it is for.** First the owner's own Unraid server (3 Plex users). It is built from day one to be published in Unraid Community Applications, like Almali Smart Cache for Plex · Unraid (PSC). Unlike PSC, it runs on any Docker host; the Unraid-specific parts (which disk holds a file) are optional.
- **Independence.** It does not depend on PSC, share its code or read its database. It reuses PSC's technology choices and visual design system only.
- **Reference.** The approved interactive mockup: [Almali Library Curator mockup](https://claude.ai/artifact/9y9BoTrmDHjjndPyuL2zmt).

## Goals, non-goals, anti-goals

Version 1 is a manual curator: the app proposes, the owner marks, the app carries out the removal safely.

**Goals (v1)**

1. Detect the categories in the Detection section, per series, per season and per movie.
2. Show each candidate with its reason, what the scope deletes, size, disk (when host paths are configured) and rating with its source, in the same place where it is acted on.
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
| Unraid host filesystem | No | Which array disk or pool holds each file, hardlink count | Size shown without disk; the card hides the disk field and freed space is marked as estimated |

- **Users.** "A user" is a Plex account with access to the server, including managed Home users. Plex lists every account the server ever shared with, often hundreds, so the counts and lists the app shows (the never-watched sentence, the sources panel, the excluded-users list in Settings) take only the users with at least one recorded play in the Plex or Tautulli history. The owner can exclude accounts (for example a guest) from all calculations; an excluded user stays in the Settings list so the exclusion can be undone. Tautulli numbers the accounts as Plex does for shared users, but the server owner is `1` in Plex and the owner's plex.tv id in Tautulli, so a Tautulli play is attributed by user id first and, when no account carries that id, by the Tautulli username compared case-insensitively with the account title. A Tautulli user that matches neither is skipped, and the scan log says how many plays were skipped for it.
- **IMDb.** IMDb has no free public API. The **Rating source** setting chooses the number the card shows and the high-rating rule reads: `critic` (the default, and the behaviour released before #128) uses Plex's critic `rating`, then TMDB; `audience` uses Plex's `audienceRating`, then the critic rating, then TMDB; `highest` uses the highest of the critic rating, the audience rating and TMDB. The card labels where the number came from (Plex, Plex audience or TMDB) and shows no source when there is no rating at all. An install upgraded without the setting behaves as `critic`.
- **Read cost.** Every scan reads the whole play history of Plex and Tautulli, because either service can store a play late with the date it happened at: a read that started from the newest stored play would skip that play for good. Reading the history again stores nothing new: plays are deduplicated on user, item, instant and position, and a stored row is only rewritten when one of its values changed, so a second scan of an unchanged library writes almost nothing. TMDB answers are kept in the database for 30 days; a series Sonarr already answered is not read at all when TMDB has nothing left to add (a rating Plex does not store, or the year of an ended series). A dry run keeps those answers, because they are external facts rather than scan results. The sources panel likewise reads the disk sets recorded with the facts they describe, so a scan or a file refresh that keeps failing leaves it accurate and fast instead of re-scanning the library on every page.
- **Credentials.** Tokens and API keys are entered in the app, stored encrypted at rest, never logged and never returned in full by any API.
- **Serving cost.** Pages themselves are never stored (`Cache-Control: no-store`). Every page links its static assets with a version token, the digest of the file's own bytes (`/static/css/hud.css?v=…`), and such a URL is cached for a year as `immutable`, so a release changes the URL instead of serving a stale asset; an unversioned or outdated URL is answered with `no-cache` and revalidates with a cheap 304. The CSS and JS travel gzipped whenever the browser accepts it, through Python's own `gzip`, so no extra runtime dependency is added: the 750 kB of CSS, JS and SVG assets leave as roughly 120 kB, and the compressed bytes are kept in memory until the file changes. SQLite opens every connection with `synchronous=NORMAL` (durable under WAL, one file system sync less per commit) and a 16 MiB page cache instead of the 2 MiB default.

## Detection categories

Five fixed categories; each candidate carries the category, the unit it applies to (series, seasons or movie) and a one-sentence reason built from the facts that made it match.

| Category | Applies to | Matches when | Default threshold | Example reason |
| --- | --- | --- | --- | --- |
| Stalled series | The unopened seasons | Someone watched part of the show, then later seasons sat with no play | No play of those seasons for 6 months | Anna watched S1 in March 2025. S2 and S3 were never opened. |
| Never watched | Series or movie | No user has played any part since it was added | Added more than 3 months ago | Added April 2024. 0 episodes played by any of the 3 users. (With no recorded play by anyone yet: Added April 2024. No episode has ever been played.) |
| Cancelled, no viewers | Whole series | Sonarr or TMDB says ended or cancelled, and no user is mid-show | Last play more than 6 months ago | Cancelled in 2022. Last play August 2023, nobody mid-season. |
| Seasons everyone finished | Finished seasons only | Every user who follows the show has watched those seasons, and later seasons are still pending | All followers finished them more than 1 month ago | Marc and Anna (the only followers) both finished S1. S2 is in progress. |
| Movies not rewatched | Movie | Watched at least once, nobody played it since | Last play more than 18 months ago | Watched once by Marc in February 2024. Nobody since. |

**Definitions**

- **Follower of a show:** a user who has played at least one episode of it. Users who never started it do not count towards "everyone".
- **Watched:** an episode or movie marked watched in Plex, or played past 90% in history.
- **Mid-show:** a follower who has watched some but not all aired episodes and played one within the stalled-series window.
- An item can match several categories; it appears once under All, with every matching reason.
- **What a scope deletes.** A whole-series row says "Whole series · N seasons · M episodes". A seasons row names its seasons in the title ("Foundation S2–S3") and says how many episodes they hold. A movie is unchanged.
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
- The high-rating rule reads the same number the card shows, chosen by the **Rating source** setting (see Data sources).

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
- A removal is refused while another library still points at the same Sonarr or Radarr entry, or at one of the files it would delete (the same movie or series indexed by two libraries, or two libraries over one file). Nothing is sent to Sonarr, Radarr or Plex: no file is deleted, the entry is not unmonitored and no import exclusion is added, because that call would take the other copy's files, monitoring and exclusion with it. The item stays in Leaving soon flagged "Removal failed", the card and History name the other library, and the owner decides which copy goes before marking it again. A removal whose entry and files no other curated row uses behaves exactly as before, and a dry run reports the same refusal without sending anything.
- Cancelling a removal that failed after Sonarr or Radarr was changed (Keep it, Undo, or a re-check exit) first restores the monitoring and removes the import exclusion the failed attempt added. The cancellation claims the removal before it calls the arr, so a retry cannot delete it meanwhile and Keep it is not offered while it runs. If the app stops in the middle, the next scan at least 30 minutes later puts the removal back to "Removal failed" with everything it still has to undo.
- "Deleted" is shown only after the app confirms the files are gone from disk. A 2xx from Sonarr or Radarr alone is not proof.
- A dry-run mode does every step except steps 3 and 5 and reports what would happen. It runs on a private copy
  of the database that carries the same constraints, so a plan it reports is one a live run could execute, and
  nothing it decides reaches the store: only its report is recorded, plus the cached TMDB answers, which are
  facts rather than decisions.

## Screens

The main screen follows the approved [mockup](https://claude.ai/artifact/9y9BoTrmDHjjndPyuL2zmt): a summary row, three tabs, and every action on the row it applies to.

| Screen | What it shows | Actions |
| --- | --- | --- |
| Library health · summary | Space in candidates, in grace, protected, freed so far; time of last scan | Run scan now |
| Library health · Candidates | Category buttons with counts and a one-line explanation; sort buttons for size, age, rating and time without plays, kept in the `sort` parameter of the URL; rows sorted by size (largest first) by default, with type, title, unit, what the scope deletes, reason, size, disk (only when host paths are configured), rating and its source | Mark for removal / Undo, Keep forever |
| Library health · Leaving soon | Marked items sorted by the soonest deletion date (or by size, age, rating or time without plays, with the same sort buttons), days left as a bar, the deletion date with how many files and how much space it deletes, re-check status, cancelled removals with their reason | Keep it (cancels the removal) |
| Library health · Protected | Protected items with what protects them, sorted by size by default with the same sort buttons | Undo "Keep forever" |
| History | Every deletion and cancellation, newest first, freed space per month | Filter, export CSV |
| Connections | Status and test of Plex, Tautulli, Sonarr, Radarr, TMDB, host paths | Edit, test |
| Settings | Thresholds, grace days, protected libraries, minimum rating, rating source, excluded users, collection name, scan schedule, dry run with what each state means | Save; turning dry run off asks first, with what is now due |
| First-run wizard | Plex sign-in, library selection, optional connections, protected libraries | Step through |

**Rules**

- Visual system: Plex Smart Cache's design system (dark HUD look, Tabler base, the same colour tokens, type and breakpoints), so both apps read as one family.
- Every badge has text and an icon; colour is never the only cue.
- The actions that arm a deletion — Mark for removal, Resume deletions, Save and allow deletions — are styled apart from the safe ones: a dark red frame and fill plus a warning icon, so the difference never rests on colour alone. Turning dry run off comes back as the same Settings form with a panel that lists what is now due (items, space, first deletion date, kill-switch state); nothing is stored until the owner confirms, exactly as resuming the kill switch already worked (#64).
- Below 992 px, the width where the sidebar becomes a fixed column, the secondary chrome folds behind disclosures: the navigation and Sources under one Menu summary, the language links under their own, the four summary cards and the scan steps under theirs, and the candidate tabs come before the cards. About and Sign out move from the top bar into that Menu, so the header's action row keeps only Run scan; Sign out stays a POST form with its CSRF token. The category filters and the tabs stay one swipeable row. From 390 px wide the first candidate row starts inside the first screen, and on a 320 px screen it is one swipe away; every link, action and guard stays in the page, and the desktop layout is untouched (#65, #113).
- A row action (Mark for removal, Undo, Keep forever, Keep it) returns to the same tab, category filter, sort, page and row it was pressed on: the row carries the view it belongs to, the page lands on it and outlines it (#63, #134).
- Works from 320 px wide with no horizontal page scroll; touch targets at least 44 × 44 px.
- UI text in English, Catalan and Spanish from day one; no hard-coded strings in templates or scripts.

## Packaging and delivery

Shipped the same way as Plex Smart Cache: one Docker image on GHCR, an Unraid installer script, and an Unraid Community Applications (CA) template, plus a docker-compose example for any other Docker host.

| Deliverable | Requirement |
| --- | --- |
| Docker image | `ghcr.io/almali-dev/almali-library-curator`, multi-arch (amd64, arm64), non-root with `PUID`/`PGID` (Unraid default 99/100), health check, signed release tags plus `latest` |
| Installer | `install-unraid.sh` modelled on PSC's v2 installer: asks for paths, port, user and password, generates the secret key, verifies its own download checksum, writes the container; `uninstall-unraid.sh` removes only what it created |
| CA template | `unraid-template.xml` with Repository, Registry, Support, Project, TemplateURL, Category, WebUI, and an **Icon that exists** (PSC's template has none, see PSC issue #850) |
| CA listing | A support thread on the Unraid forum and the template repository submitted to CA, as CA requires; to be confirmed against CA's current submission rules before release |
| Docs | README, Unraid deploy guide, CHANGELOG from the first release |

**Container mounts**

| Container path | Host default | Mode | Why |
| --- | --- | --- | --- |
| `/config` | `/mnt/user/appdata/almali-library-curator` | rw | Settings, database, logs, backups |
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

The app is named Almali Library Curator for Plex, and its sibling is named Almali Smart Cache for Plex · Unraid, following Plex's guideline of "\<Name> for Plex" instead of "Plex" inside the name. Both live in the `almali-dev` organization, whose name is the shared family prefix.

- **What the guidelines say.** The [Plex trademark guidelines](https://www.plex.tv/about/privacy-legal/plex-trademarks-and-guidelines/) and the [developer guidance](https://www.plex.tv/?p=43) allow "\<Name> for Plex" when the name is unique, and prohibit Plex or derivatives in the app's own name, domains and trademark filings. Plex has enforced this before (the MK Plex add-on was withdrawn after a legal letter, [BetaNews](https://betanews.com/2017/09/01/mk-plex-kodi-addon-dies)).
- **Unraid stays out of this name.** Almali Library Curator runs on any Docker host. Almali Smart Cache for Plex · Unraid is Unraid-only, which its description, CA category and README say in their first line.
- **Family prefix: Almali (decided Nov 3, 2026).** The shared word in front of both names is "Almali", and the projects are published under the `almali-dev` organization on GitHub.
- **Before release.** Check both names for clashes on GitHub, Docker Hub and Unraid CA.
- **Disclaimer.** The README, the About page and the CA template state that the app is not affiliated with or endorsed by Plex Inc.

**Open questions**

- [x] Whether to add a shared family prefix (for example "Tower") to both names — answered: "Almali", applied on Nov 3, 2026.
- [x] Which rating to show by default: Plex's stored rating, TMDB, or both — answered: the **Rating source** setting, `critic` by default (Plex critic then TMDB), or `audience` or `highest` (S2, #128).
- [ ] Per-library thresholds in v1, or global only (this document assumes global).
- [ ] Should a Plex user other than the owner be able to see the app or the Leaving soon reasons?
- [ ] Design of the automatic marking mode (after v1).
