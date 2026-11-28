# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While pre-1.0, every completed task bumps the minor version (see the
[roadmap](docs/ROADMAP.md)); the first publishable release is v1.0.0.

## [Unreleased]

### Added

- The Plex client can remove one version of an item (#133): it sends
  `DELETE /library/metadata/{key}/media/{id}` only after reading the item
  again. The version must still be the stored file, a single part, and
  another version with a file of its own must remain. It is not wired to
  any removal yet. Removing a whole item still refuses several versions.

## [0.50.0] - 2026-11-27

Upgrade notes: schema migration 020 adds nullable columns for the IMDb score
and the Tomatometer. They fill on the next scan. Which scores appear depends on
each Plex library's "Ratings Source" setting.

### Changed

- The IMDb link is only the yellow IMDb button, now with the IMDb score
  inside when Plex sent one, for example "IMDb 7.9" (#162). The frame and
  the arrow of v0.49.0 are gone. The button still lifts on hover, shows a
  focus ring and keeps its 44 px touch target, and its name and tooltip say
  the score.

### Added

- A red Rotten Tomatoes button beside it, with the Tomatometer when Plex
  sent one, for example "93%" (#162). Plex sends no Rotten Tomatoes id, so the
  button opens a Rotten Tomatoes search for the title; a seasons row searches
  for its series. It always appears when the item has a title, without a
  percentage when the score is unknown. Only addresses the app builds are
  ever rendered.
- The sync keeps the IMDb score and the Tomatometer, from Plex only: its
  `Rating` entries when the payload carries them, else the
  `ratingImage`/`audienceRatingImage` attributes. Any other source (TMDB,
  the Rotten Tomatoes audience score) is never taken for these two.
  Migration 020 stores them, and the cards of a dry run get them through the
  published rows.

## [0.49.0] - 2026-11-27

### Changed

- The IMDb link on every card shows the IMDb logo, and it looks clickable
  (#153 follow-up). An "opens elsewhere" arrow sits next to the logo. On
  hover the link lifts and gets a cyan frame, the keyboard focus gets a
  visible ring, and a tooltip names the title. The link keeps its accessible
  name and its 44 px touch target, and reduced motion turns the lift off.

## [0.48.0] - 2026-11-27

Upgrade notes: schema migration 019 adds indexes only. Forgetting a Sonarr, Radarr,
Tautulli or TMDB key now asks for confirmation first.

### Added

- The outcome of a row action is visible where the page lands (#147). Mark,
  Undo, Keep forever, Keep it and a refused action keep their flash message in
  a sticky region at the top of `<main>`, so the redirect to the row anchor no
  longer scrolls the answer off screen; the region has a close button for a
  reader with scripts, and a success is still a polite `status` while an error
  is an assertive `alert`.
- The page follows a running analysis (#147). While a scan runs, the status
  box polls a small JSON endpoint behind the owner login and the page reloads
  once when the scan finishes or fails, so the outcome and the list it filled
  appear as the copy promised. The endpoint answers only the status word --
  never a report, a step, a token or a path -- and it is not a public route.
  Without JavaScript the three status messages now say to reload the page.
- The Plex sign-in moves on by itself (#147). When the poll sees that the
  sign-in is no longer pending (authorized, expired, failed or cancelled), the
  page reloads once to show the next step -- the server picker, or the message
  -- instead of leaving the PIN, the link and "Cancel" on screen.
- The dry-run-off answer says nothing was saved yet (#147). It carries a
  notice at the top of the Settings form with a link to
  `#dry-run-off-heading`, and the notice takes the focus; the panel itself is
  unchanged.
- Forgetting a Sonarr, Radarr, Tautulli or TMDB key asks first (#147), through
  the same confirmation page as forgetting the Plex token. Only the confirmed
  step deletes the key; cancel and an unconfirmed POST delete nothing.

### Changed

- The Library page reads its database in a constant number of statements,
  whatever the number of marked items (#148). Leaving soon is one joined
  query plus one grouped read of the episodes, instead of about four
  statements per open request; the curated scope is resolved once per request
  instead of six times; the sources panel is built once instead of twice; and
  the recent cancellations are read only by the tab that shows them. Measured
  on a seeded store of 2,700 items, 18,000 episodes and 60 open requests, one
  Library build went from 459 statements to 17 (the Leaving soon tab from 451
  to 14), and the page no longer grows with the number of requests: 3 and 30
  requests now cost the same 20 statements over HTTP.
- A scan writes its display snapshot in two statements instead of about three
  per candidate (#148): 400 candidates went from 1,000 statements to 2. A
  seasons removal reads Sonarr's episode files once instead of twice, without
  changing which files it deletes.
- Migration 019 adds indexes only (#148): `episodes(file_path)` and
  `media_items(file_path)`, `media_items(radarr_id)` and
  `media_items(sonarr_id)` where the id is not NULL, and `media_items(id)`
  where `plex_present = 0`. The shared-use check of #136 went from 0.75 ms to
  0.007 ms on 21,000 items and the "is any item proven absent" probe from
  2.0 ms to 0.002 ms on 50,000 items. They can be dropped and rebuilt: no
  stored row and no query result changes.
- The poster a dry run stores in its snapshot is found by index (#148): the
  lookup names the two kinds of row it accepts, so it searches
  `idx_scan_rows_item` instead of scanning every published row (20,000 rows:
  0.021 ms to 0.005 ms).
- Slow saves and probes say they are working (#147). The Plex save, the
  service key save, the connection test and the analysis are forms marked
  `data-busy`: submitting one disables its own submit button and sets
  `aria-busy="true"` with a translated "working…" label until the page
  unloads, and no other form is blocked. The connection test is a GET form
  button instead of a link so it can be marked the same way. Without
  JavaScript nothing changes.

### Fixed

- An Undo no longer walks the whole backlog of pending collection cleanups
  (#155). It removes its own request's member only. The cleanups an earlier
  Plex failure left behind stay with the retry the next scan already runs, so
  nothing is lost. Before, one click against a hung Plex cost one bounded call
  for every pending cleanup.
- More calls a web request waits on make one bounded attempt (#155). The
  Sonarr and Radarr revert an Undo performs, and the TMDB lookups of a Mark's
  fresh check, now use one attempt with a timeout of at most five seconds,
  instead of the default ladder of three retries with a 15-second timeout. A
  failure keeps today's outcome and messages: a revert that fails still
  refuses the Undo and keeps the record of what is applied, and a TMDB
  failure leaves the rating as the cache or Plex has it. Scans and the
  deletion executor keep their retries.

- Accessibility (#148). Every row's buttons (Keep forever, Mark for removal,
  Undo, Keep it) and every service card's actions point with
  `aria-describedby` at their own item or service, so a screen reader or voice
  control can tell the rows apart (WCAG 2.4.6). The visible words are
  unchanged. "Resume deletions" is styled as a danger action from its first
  button, like its confirmation page. Button borders use the control border
  colour, which reaches the 3:1 non-text contrast minimum (WCAG 1.4.11).
- docs/UNRAID.md and the README explain how to update a template install by
  hand: **force update** in the Docker tab's Advanced View (or Edit → Apply),
  because "Check for updates" may not notice a new `latest`.

### Security

- The uninstaller no longer decides a root-run deletion on an unvalidated media
  path (#156). Before the guard that keeps the app data directory away from the
  media share uses the manifest's `media` value, the uninstaller refuses the
  one value no installer run can write - empty, relative, or containing `:` or
  `,` - because that means a forged or corrupt manifest: the run names the
  value, exits non-zero before anything is removed and deletes nothing. A
  forged value can therefore no longer make the run believe the app data is
  safe to delete. Every spelling the installer records is accepted, and the
  guard resolves it: a trailing slash, a `..`, a symlinked share and an
  `ALC_INSTALL_UPGRADE_MEDIA` answer uninstall exactly as before, so an
  existing installation is unaffected.

## [0.47.0] - 2026-11-27

### Added

- Every item card links to IMDb (#153). Candidates, Protected, Leaving soon
  (open and cancelled), Duplicates and History show an IMDb link: the item's
  title page when Plex reports an IMDb id, otherwise IMDb's own search for the
  item's title and year, URL-encoded. A seasons row searches its series, never
  "1923 S2", and a row with neither an id nor a title shows no link at all.
  The address is built by the app's one helper and opens in a new tab. A dry
  run's links travel through the published snapshot, as its posters do, and
  the CSV export keeps its published columns.

### Changed

- The card posters are bigger (#153): 128x192 from a 480 px viewport, 64x96
  below it so the title and the reason still fit at 320 px. The poster route
  still asks the transcoder for 240x360; the same image is simply shown larger.
- Migration 018 adds a nullable `imdb_url` to the Library's cached scan rows,
  additively, and recreates the publish triggers so a dry run's links reach the
  screen (#153). Rows published before it show no link until the next scan
  publishes again; nothing stored is rewritten.

## [0.46.0] - 2026-11-27

Upgrade notes: after this version plex.tv lists the app as a new device (one
identifier per installation, #145); the old shared entry can be removed under
**Authorized Devices**. An Unraid `--upgrade` with no running container to
compare against now needs `ALC_INSTALL_UPGRADE_MEDIA=<media share>` (#146).

### Added

- History cards show the item's Plex poster too (#141). They use the same
  `/poster/<id>` route, rules and placeholder as the Library cards (#139). An
  event whose item is gone, or has no usable thumb, shows the placeholder.
  The CSV export keeps its published columns.

### Fixed

- Mark and Undo no longer wait on a hung Plex (#144). Every collection write a
  web request makes -- the list-then-create of the Leaving soon collection, one
  membership add per item, and the removal an Undo performs -- is a single
  attempt with a timeout of at most five seconds, and the process-wide
  collection lock is never waited on longer than that. The image runs one
  worker with four request threads, so a hanging server used to hold a thread
  for about a minute per call while the next clicks queued behind the lock. A
  write that still fails behaves exactly as before: nothing is claimed as
  marked, the durable removal request stays in place, and the retry the next
  scan already runs adds the missing member or finishes the cleanup. Scans and
  the deletion executor keep their retries.
- The Connections test, and `/setup?test=1`, answers in about ten seconds even
  when every service hangs (#144). Each probe is one attempt with a five-second
  timeout instead of the client's default retry ladder, and the configured
  services are probed at the same time instead of one after another. The
  statuses and their messages are unchanged.

### Security

- The root-run installer and uninstaller no longer trust
  `install-manifest.json` on its own (#146). The manifest lives in the app
  data directory, which the container can write, and the directory belongs to
  `PUID`:`PGID`, so a container process can replace the file whatever its
  owner is: root ownership alone proves nothing. The entrypoint therefore
  keeps the manifest root-owned (mode 0600) instead of handing it to
  `PUID`:`PGID`, an upgrade gives a manifest an older image had handed over
  back to `root`, and the scripts never trust its content: every path it
  names is validated before use, so a rewritten manifest cannot make them
  mount or delete anything outside what the installer created. An upgrade
  binds only the media share a running container really mounts at `/media`;
  a recorded path that differs, and a container that is gone or mounts
  nothing there, are refused before anything is stopped or bound, and the
  share is then chosen on purpose with the new `ALC_INSTALL_UPGRADE_MEDIA`. A
  symlinked manifest is refused instead of followed. The uninstaller removes
  the environment file, the password file and the signing key only when the
  manifest names exactly the files the installer creates: a path outside the
  app data directory, another name, a `..` that climbs out, a symlink that
  leaves it, a directory-looking value or a last component that is a link is
  refused, and nothing (not even the container) is removed. It removes a
  container only when it carries the installer-managed label, so a manifest
  without `install_attempt` can no longer remove a container this installer
  did not create. The published `install-unraid.sh.sha256` and
  `uninstall-unraid.sh.sha256` are regenerated.
- The app announces one **Plex client identifier per installation** instead
  of a name shared by every copy of it (#145). It is 128 random bits, created
  on first use and kept owner-only (mode 0600, written atomically) in
  `$ALC_CONFIG_DIR/plex_client_identifier`; the sign-in and every Plex client
  of one installation send the same value. Every installation looked like the
  same device on plex.tv, and a pending PIN could be polled with an identifier
  anyone could guess. A missing, unreadable or corrupt file is replaced with a
  fresh identifier and a warning, and never stops the app. An existing install
  keeps its stored token: only the identifier file is added. After the
  upgrade, plex.tv lists the app as a new device, and the old shared entry is
  no longer used; it can be removed under **Authorized Devices**.
- Request bodies are capped at **1 MiB** (#145). Every form is a small
  URL-encoded post and no route reads an upload, so an oversized body is
  refused with HTTP 413 while it is parsed, before the CSRF check or the login
  check reads the form, and nothing is spooled to the container's small `/tmp`
  tmpfs. The refusal is the app's own error page, in the reader's language,
  like the other error pages.
- The login brute-force guard groups **IPv6 clients by /64** and adds a
  **process-wide ceiling on the failures of one window** (#145). An attacker
  rotating IPv6 addresses behind a reverse proxy bought five guesses per
  address; the /64 makes it five per client, and once 100 failures are
  recorded in one 15-minute window every sign-in attempt is refused with HTTP
  429, whichever address it comes from, until the window drains. The owner is
  never held out for longer than the window, the per-address limit and its
  saturation behaviour are unchanged, and nothing sleeps on a request thread.

## [0.45.0] - 2026-11-27

### Added

- Item cards show the Plex poster (#139). Candidates, Protected, Leaving
  soon and every Duplicates group show the item's poster beside the title,
  and a seasons row shows its series' poster. The image is served by the app
  itself at `/poster/<id>`, behind the login. The browser never learns the
  Plex address or the token, and the CSP is unchanged. Only a thumb the sync
  stored, with the exact shape Plex sends (`/library/metadata/<n>/thumb/<n>`),
  is ever requested, and only through the Plex client, which never follows a
  redirect. The poster is a 240×360 image from Plex's photo transcoder (or
  the raw thumb when the transcoder does not have it), cached under
  `/config/posters` and for a day in the browser. An item with no poster gets
  a neutral placeholder of the same size, and so does a poster Plex cannot
  serve. A Plex outage is answered at once: one attempt per poster, then a
  minute of placeholders, so the pages and the healthcheck never wait for
  Plex. Posters appear after the next scan, including a dry run.

### Changed

- Schema migration 017 adds a nullable `thumb` column to the stored items and
  to the Library's rows (#139). It is additive: an upgraded database keeps
  every row, and the column stays empty until the next scan fills it.

## [0.44.0] - 2026-11-26

### Added

- The Candidates, Protected and Leaving soon lists sort by size (largest
  first), age (added longest ago), rating (lowest first) or time without plays
  (longest first) (#134). The choice is a `sort` parameter in the URL and a row
  of buttons on the list, so a refresh, a shared link, the pager, a category
  filter, a tab and the return from a row action rebuild the same ordered list.
  Each list keeps its current order as the default -- Candidates and Protected
  by size, Leaving soon by the soonest deletion date -- and an unknown or
  missing value falls back to it without an error. The buttons are translated
  in English, Catalan and Spanish and keep their 44 x 44 px targets at 320 px.
- A read-only **Duplicates** screen (#132). It groups a title stored in more
  than one curated library, matched by the TMDB, IMDb or TVDB id Plex already
  reports (never by the title alone), and the items that hold more than one
  version. Every copy or version says its library, resolution, video codec,
  size and bitrate, read from the data Plex already returns; a fact Plex did
  not send is shown as unknown, never as zero. A copy a protected library or a
  "Keep forever" keeps stays in the list and says so. The screen marks
  nothing, deletes nothing and changes no freed-space figure: deleting a
  version is #133. It reads the curated libraries only, like the other
  screens, is translated in English, Catalan and Spanish, and is reachable
  from the sidebar at `/duplicates`.
- The sync now stores every version of every item (#132), not only the first file's
  path and size. Schema 16 adds the `media_versions` table: one row per Plex
  `Media` entry with its own resolution, video codec, bitrate (kbps), file and
  size, where a stacked version counts all of its parts. The migration is
  additive and nullable, so an upgraded database keeps every stored row and
  its item rows keep their first file and size; the table starts empty and the
  next scan fills it. A version Plex no longer lists leaves the store in the
  same transaction that publishes the snapshot, so the table never describes a
  file that is gone. The screen shows at most the first 100 groups of each
  list (largest first) and says so when it cuts one, and it covers items
  (movies and series), not episodes.
- A sixth detection category, **Finished seasons of a running series** (#135).
  For a series that is still running — not ended, cancelled or stalled, the
  shapes the existing categories already cover — it proposes only the seasons
  every counted user finished, and leaves the newer ones alone. The latest
  season everyone finished is always kept, so a series keeps the season a
  rewatch would need before the new one arrives, and a season someone is still
  watching is never proposed. It reads the watched rule, the counted users and
  the season sizes exactly as the seasons everyone finished category does, so
  the 90% rule lives in one place. It holds its own threshold, months since
  everyone finished those seasons (3 by default, separate from the 1 month of
  the seasons everyone finished category), appears as its own filter and badge
  reason in English, Catalan and Spanish, and uses the same protections and the
  same season-scoped Leaving soon lifecycle as the other seasons category.
- A candidate card says what its scope would delete (#128). A whole-series
  row shows "Whole series · N seasons · M episodes"; a seasons row names
  its seasons in the title and says how many episodes they hold. The text
  is translated in English, Catalan and Spanish, and a movie card is
  unchanged.

### Changed

- The card and the high-rating protection follow a new **Rating source**
  setting (#128). `critic`, the default and the behaviour released before
  this change, uses Plex's critic `rating`, then TMDB. `audience` uses Plex's
  `audienceRating`, then the critic rating, then TMDB. `highest` uses the
  highest of the critic rating, the audience rating and TMDB. The source label
  names where the number came from (Plex, Plex audience or TMDB) and shows no
  source when there is no rating. An upgraded install whose settings file has
  no key behaves as `critic`, and a change takes effect on the next scan.
- The card hides the disk field when the host filesystem is not configured
  (`UNRAID_ROOT` empty) instead of always showing "Unknown" (#128). When it
  is configured, the disk shows as before. The Leaving soon card follows the
  same rule.

### Fixed

- A removal is refused while another library still uses its target (#136).
  Two Plex items (the same movie in "Movies" and "Movies 4K", a series indexed
  by two libraries, or two libraries over one file) can link to the same
  Radarr or Sonarr entry or point at the same file. A Sonarr series removal
  then deleted the other library's files and still recorded `deleted`, and a
  shared Radarr movie lost its monitoring and gained an import exclusion for
  the other copy. The deletion preflight, in the dry-run preview and in the
  real run, now refuses before anything is sent to Radarr, Sonarr or Plex
  when another item uses the same Radarr or Sonarr entry on that route
  (`shared_arr_entry`) or a file of the marked scope, whatever the route
  (`shared_file`). Nothing is deleted, unmonitored or excluded, the card and
  History name the other library in the reader's language, and a removal
  nothing else shares behaves exactly as before.
- An invalid `ALC_AUTH_PASSWORD_HASH` now gets its recovery hint, on the
  closed page and in the startup log, even when `ALC_AUTH_USER` is missing
  (#115). Before, the app answered the generic "login is not configured"
  page and logged nothing about the hash, so fixing the user still left a
  closed app.

## [0.43.0] - 2026-11-25

### Changed

- Every scan reads the whole play history of Plex and Tautulli instead of only
  what followed the newest play already stored (#112). Plex was read newest
  first and the read ended at the newest stored play; Tautulli was asked only
  for the days since the newest imported one. A play either service stores
  late with the date it happened at, such as an offline play synced
  afterwards, was older than that point and was skipped for good. Both
  histories are now read whole on every scan, so such a play is imported on
  the next scan. Reading the same history again adds no play and changes no
  count: play events are deduplicated on user, item, instant and position, and
  a stored row is still rewritten only when one of its values changed. The
  history of a large library is therefore read in full on every scan, which
  costs more Plex and Tautulli requests than the incremental read did. The
  stored history cursors are no longer read or written; an upgraded install
  keeps the old rows in `app_meta` unused, with no migration.
- On a phone, About and Sign out move from the top bar into the existing Menu,
  so the candidate list starts higher. Sign out stays a POST form with its
  CSRF token, every target keeps its 44 × 44 px size, and the desktop layout
  is unchanged (#113).

### Fixed

- An invalid `ALC_AUTH_PASSWORD_HASH` still keeps the app closed (#115), but
  the owner can now see why and how to recover. The hash wins over the file
  and the plain password, so an owner who switched back to
  `ALC_AUTH_PASSWORD` without removing the hash stayed locked out of every
  screen with only a log warning. The startup log line now names
  `ALC_AUTH_PASSWORD_HASH` as invalid and the recovery, and the closed screen
  (HTTP 503, the same status as before) has its own translated page in
  English, Catalan and Spanish: fix the hash, or remove the variable so the
  plain `ALC_AUTH_PASSWORD` or the `ALC_AUTH_PASSWORD_FILE` file is used, then
  restart the container. Neither the log nor the page shows the hash, the
  password or any other secret, and nothing opens without valid credentials.

## [0.42.4] - 2026-11-25

### Added

- The Publish workflow proves each release pulls without a login (#114, #42).
  A last job with no package permission and no registry login pulls the
  released version for linux/amd64 and linux/arm64 from an empty Docker
  config, so a package that is private again fails the release run instead
  of breaking the anonymous Docker, compose and Unraid installs. The README
  now says the package is public.

## [0.42.3] - 2026-11-24

### Fixed

- A Tautulli history row that reports an empty `media_index` or
  `parent_media_index` no longer fails the whole import (#125). Tautulli sends
  `''` in those fields for movies, which have no episode or season number:
  754 of the owner's 4,494 rows. One such row refused the entire history page,
  so no Tautulli play was imported at all. An empty string is now read as the
  no value those optional numbers already mean, so a movie row imports like an
  episode row.
- A scan whose enrichment step failed on a refused payload no longer reports
  the run as finished with no errors (#126). The report kept an error only
  when it was a bare class name, so the #120 detail
  (`ValidationError: <Model> <field> <type>`) was dropped: one run said
  `finished`, `errors: {}` while the log said its Tautulli history import had
  failed, and the UI showed nothing wrong. The report keeps that diagnostic
  now, so the step, the run status and the reported error agree with the log.
  It still names the model, the field and the error type and never the refused
  value. An error body that comes from a service is still never stored, but
  its step is no longer hidden either: it is reported as failed with the
  placeholder `Error`.
- The owner's Tautulli plays are imported again (#127). Tautulli numbers the
  server owner with the owner's plex.tv id, while Plex `/accounts` calls it
  `1`, so none of the owner's plays matched an account and all of them were
  skipped, probably the heaviest viewer's. When a Tautulli `user_id` matches no
  account, its username is now compared case-insensitively with the account
  title; an account id still decides first when both could apply. A Tautulli
  user that matches neither is still skipped, and
  `enrichment_history_finished` now names how many plays were skipped for that
  user (the ten heaviest, so the line stays bounded).

## [0.42.2] - 2026-11-24

### Fixed

- The never-watched reason counts the people who watch, not every Plex
  account the server ever shared with (#122). It said "by any of the 356
  users" on a server with a handful of viewers. The count, the sources panel
  and the excluded-users list in Settings now take only users with at least
  one recorded play in the Plex or Tautulli history, and excluded users are
  never counted. An excluded user stays in the Settings list so the
  exclusion can be undone. When nobody has played anything yet, the sentence
  drops the number ("Added April 2024. It has never been played.") and the
  item still appears. Reasons stored by an earlier scan keep their old
  number until the next scan.
- The container shows the app icon on the Unraid Docker tab instead of the
  default "?" (#123). The template's `<Icon>` now points at a committed
  256×256 PNG, and the image carries `net.unraid.docker.icon` and
  `net.unraid.docker.webui` labels, so a container created by
  `install-unraid.sh` shows the icon too. A container created before this
  release gets it when it is recreated on the new image (a Watchtower update
  or a reinstall).

## [0.42.1] - 2026-11-23

### Changed

- A scan step that fails on a refused payload now says which one (#120).
  Instead of a bare `ValidationError`, the scan report and the
  `scan_step_failed`, `scan_failed`, `enrichment_step_failed` and
  `enrichment_link_failed` log lines name the model and, for at most three
  errors, the field and the error type, for example
  `ValidationError: PlexMetadata ratingKey missing`. The refused value is
  never included, and a location part that is not a declared field of the
  refused model, such as a dictionary key taken from the input, is masked as
  `?`. Every other failure is still named by its class only.

### Fixed

- A Plex watch history entry without a `ratingKey` no longer fails the whole
  sync (#118). Plex keeps the plays of an item removed from the library but no
  longer links any item to them; one such entry made every scan stop at the
  sync step with a bare `ValidationError`, so detection found no candidates.
  The entry is now skipped, since it cannot match any library item, and its
  instant still moves the history cursor. Library items keep requiring their
  key.
- A Tautulli history row with a null `id` or `rating_key` no longer fails the
  whole history import (#119). The row `id` is never stored, so such a row is
  imported like any other; a row without `rating_key` names no item and is
  counted as `skipped_invalid`. Every other row is still imported.

## [0.42.0] - 2026-11-22

### Changed

- Connections is one card per service, in the order of the first run: Plex
  first and marked as the required connection, then Tautulli, Sonarr, Radarr,
  TMDB and the host paths. Each card carries its own state (connected,
  configured but not tested, not configured, or unreachable with a line that
  says what to check), the one line of what the service adds, its actions and
  its own form, so nobody has to scroll to configure what they just saw
  missing. Only the form folds, through a native exclusive accordion: one card
  open at a time, no script needed, and the server renders the card open when
  a save is refused or the link names it (`/connections?service=sonarr`,
  `/connections#sonarr`). A service managed by its environment variables shows
  its note where the form would be, and the Plex sign-in lives inside the Plex
  card. A single test of every connection sits at the top, with the time it
  ran and how many services answer. The first-run checklist links straight to
  the connection it names.

## [0.41.0] - 2026-11-22

### Added

- The home page lists the five steps of the first run, each with its own state
  and one action: Plex (connected, with the server), the first analysis (never
  run, running, failed with its step and a retry, or finished with its date),
  the managed libraries, the optional connections with what each one adds, and
  the current mode with the way to Settings. The list is shown while a required
  step is open and disappears once Plex is connected and the first analysis has
  finished, so the page becomes the candidate list it always was; the optional
  steps never keep it on screen.
- The scan status has a "never analysed" state, which used to render nothing.

### Changed

- The header of every screen names the mode (dry run or real deletion), the
  last analysis and the next scheduled analysis, so the state of the install is
  visible without opening another screen.
- Before the first finished analysis the Candidates, Leaving soon and Protected
  cards show a dash and "Pending the first analysis" instead of a zero; the
  candidate list says whether the library was never analysed (and offers the
  analysis) or was analysed and has nothing to propose.

## [0.40.0] - 2026-11-22

### Changed

- Pages link every static asset with a version token, the digest of the
  file's own bytes, and such URLs are cached for a year as `immutable`; an
  unversioned or outdated URL revalidates with a 304 instead. CSS, JS and SVG
  travel gzipped when the browser accepts it (Python's own `gzip`, no new
  dependency): about 748 kB leave as about 122 kB. SQLite runs with
  `synchronous=NORMAL` when the database is under WAL (a database left on a
  rollback journal keeps `FULL`) and a 16 MiB page cache (#110).
- The Plex sync marks absent only the items and episodes of a complete
  section that it did not see, instead of marking the whole section absent
  and restoring it, and leaves an already absent row alone: a second sync of
  an unchanged library rewrites nothing (#111).
- The Unraid template opts into Watchtower updates again with the label
  `com.centurylinklabs.watchtower.enable=true`, keeping the #71 run
  options: a template install follows the newest stable release on its own,
  as Plex Smart Cache does. The Unraid guide documents a label-scoped
  Watchtower (a maintained fork, since `containrrr/watchtower` is archived
  and fails on Docker 29) and CA Auto Update Applications, and what an
  automatic update does not do (signature check, pre-upgrade backup).

## [0.39.0] - 2026-11-18

### Added

- Destructive actions have their own danger style with a warning icon,
  each Leaving soon row says when it is deleted, how many files and how
  much space, and turning dry run off now shows what is due and asks for
  a confirmation before anything is saved (#64).
- The password can come from `ALC_AUTH_PASSWORD_HASH` or
  `ALC_AUTH_PASSWORD_FILE` instead of the plain variable; an unreadable
  file or an invalid hash refuses every login. The Unraid installer keeps
  the password in a private 0600 file instead of the environment (#71).

### Changed

- A row action in the Library returns to the same tab, filter, page and
  row instead of the top of Candidates (#63).
- On screens narrower than 992 px the navigation, sources, language,
  summary cards and scan steps fold away and the tabs come first, so the
  list starts within the first screen on a phone; desktop is unchanged
  (#65).
- Scans read only the Tautulli and Plex history added since the last
  scan, no longer rewrite unchanged rows, and keep TMDB answers for 30
  days instead of one scan (migration 015 adds the TMDB cache and two
  indexes; the pre-migration backup applies). A play recorded later with
  an older date than the last scan is not imported (#74).
- The sources panel keeps its recorded disk sets while Plex or the file
  refresh keep failing, instead of falling back to the full scan on every
  page (#36).
- The container runs with every capability dropped except the four the
  entrypoint needs, `no-new-privileges` and a read-only root file system;
  the base image is pinned by digest and watched by Dependabot, and the
  Unraid template no longer enables Watchtower updates. A request with
  `X-Forwarded-For` while `ALC_TRUSTED_PROXIES` is unset is logged once
  (#71).
- The README starts with a pull-and-run compose block on the published
  image, the compose example uses the published image with the Unraid
  mount optional, and a failed pull explains package visibility and
  `docker login ghcr.io` before anything is created (#42).

### Fixed

- A dry run checks foreign keys like the live database, so it no longer
  reports success for a write the store would refuse. The preview stays
  an in-memory copy: a rollback preview would block the web app's writes
  during a scan (#15).

## [0.38.0] - 2026-11-17

### Changed

- The image runs Gunicorn 27.0.1 (the package now accepts Gunicorn 25
  to 27) and the development tools use ruff 0.17.5. Gunicorn 27 answers
  400 to requests it used to accept loosely, such as a
  `Transfer-Encoding` other than `chunked`, a raw non-ASCII path or a
  duplicate `Host` header (#108, #109).

### Fixed

- Stopping or restoring the monitoring of a movie in Radarr writes only
  the monitored flag, through the movie editor, instead of the whole
  movie, so a change another client made in the meantime is no longer
  written back (#106).
- A removal puts each Radarr or Sonarr change on record before making
  it, so a crash or restart between the call and the record no longer
  leaves a change a later cancel would miss. Undoing a change that never
  landed does nothing, and an import exclusion is found again by its
  TMDB or TVDB id (#107).

### Security

- The Unraid installer installs the image version it was released with
  instead of `latest`, and the image signature check is no longer
  optional: without `cosign` on the host it downloads cosign v2.6.5 and
  checks it against a digest written into the script, and it stops before
  creating anything when the download, the digest or the signature fails.
  `ALC_INSTALL_SKIP_SIGNATURE=1` skips the check with a warning for hosts
  without internet; `ALC_INSTALL_REQUIRE_SIGNATURE` is still accepted. An
  `--upgrade` keeps the image the installation recorded. Release tags now
  sign the installer itself with `cosign sign-blob`, and the signature
  bundle is published as `ghcr.io/almali-dev/almali-library-curator:installer-<version>`;
  docs/UNRAID.md shows how to verify it (#56).

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

[Unreleased]: https://github.com/almali-dev/almali-library-curator/compare/v0.50.0...HEAD
[0.50.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.49.0...v0.50.0
[0.49.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.48.0...v0.49.0
[0.48.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.47.0...v0.48.0
[0.47.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.46.0...v0.47.0
[0.46.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.45.0...v0.46.0
[0.45.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.44.0...v0.45.0
[0.44.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.43.0...v0.44.0
[0.43.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.42.4...v0.43.0
[0.42.4]: https://github.com/almali-dev/almali-library-curator/compare/v0.42.3...v0.42.4
[0.42.3]: https://github.com/almali-dev/almali-library-curator/compare/v0.42.2...v0.42.3
[0.42.2]: https://github.com/almali-dev/almali-library-curator/compare/v0.42.1...v0.42.2
[0.42.1]: https://github.com/almali-dev/almali-library-curator/compare/v0.42.0...v0.42.1
[0.42.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.41.0...v0.42.0
[0.41.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.40.0...v0.41.0
[0.40.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.39.0...v0.40.0
[0.39.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.38.0...v0.39.0
[0.38.0]: https://github.com/almali-dev/almali-library-curator/compare/v0.37.0...v0.38.0
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
