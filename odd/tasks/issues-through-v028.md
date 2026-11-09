# Issue-first releases through v0.28.0

Authorized scope: v0.21.0–v0.28.0; branch `feature/issues-through-v028`; source feature: `odd/tasks/almali-library-curator.md`.
Runner: repository `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q`; delivery: separate work-unit commits and milestone tags, no monolithic PR. Forecast: 1,500–2,500 authored lines across independently reviewable units (~400 lines per slice); generated lock data excluded.

## Specs

### S1 — Owner authorization and version boundaries

> En quant a les Issues, fes primerr les més ràpides. Si cal fes mes versions abans d'arribar a la versió 1.0. Crea un Roadmap de versions i les revisem.

> Continua fins al v0.28.0

> Continua. Estàs ara en mode automàtic en principi fins la v0.28.0

Approved ladder from the preceding conversation: v0.22.0 #21/#24–29; v0.23.0 #17/#22; v0.24.0 #16/#18–20/#23; v0.25.0 #33/#30/#31; v0.26.0 #32; v0.27.0 #13/#14/#11; v0.28.0 #3/#10. Existing v0.21.0 remains subject to its native review disposition. v0.29.0+, #12/#15, T17/T18 and v1.0.0 are outside this authorization. Labels are tentative issue reports, not confirmed defects. Preserve earlier feature specifications; proposals inside issue bodies are implementation options, not permission to weaken existing safety.

### S2 — #21: ux: the first-run link on Library health is smaller than 44 x 44 px

Source: https://github.com/almali-dev/almali-library-curator/issues/21 (open when fetched). Exact issue text:

> `web/templates/web/library.html:24` is a plain `<a>` inside `.first-run`, not covered by the 44 px rule in `static/css/hud.css:135`. On a fresh install it is the most important tap and the smallest target (S7 / T10 rule).
> 
> **Suggested fix:** give it `class="btn"`.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S3 — #24: ux: the Plex server picker has no legend

Source: https://github.com/almali-dev/almali-library-curator/issues/24 (open when fetched). Exact issue text:

> `_services.html:52-57`: the `<fieldset>` of server radios has no `<legend>`; the instruction sits in a paragraph before it. Add a legend or `aria-labelledby`.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S4 — #25: ux: the disabled Protected checkbox gives no visible reason

Source: https://github.com/almali-dev/almali-library-curator/issues/25 (open when fetched). Exact issue text:

> `settings.html:84-86` and `setup.html:44-47` disable Protected for a library that is not curated, but the rule text only appears after a failed submit. Show one hint line next to the disabled control.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S5 — #26: ux: Catalan and Spanish category names differ between the filters and Settings

Source: https://github.com/almali-dev/almali-library-curator/issues/26 (open when fetched). Exact issue text:

> es: "Series estancadas" (filter) vs "Series detenidas" (threshold), "Nunca vistos" vs "Nunca vistas"; ca: "Mai vistos" vs "Mai vistes". Use one term per category per locale so owners can match a threshold to its category.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S6 — #27: ux: Spanish Protected tab is singular

Source: https://github.com/almali-dev/almali-library-curator/issues/27 (open when fetched). Exact issue text:

> es `nav.protected` is "Protegido" while `summary.protected` is "Protegidos" (and ca uses "Protegits"). Use the plural.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S7 — #28: ux: Catalan skip link uses the Valencian imperative

Source: https://github.com/almali-dev/almali-library-curator/issues/28 (open when fetched). Exact issue text:

> ca `nav.skip` is "Ves al contingut"; the standard Central Catalan used elsewhere in the catalog is "Vés al contingut".
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S8 — #29: ux: "Open the Plex sign-in" leaves the app in the same tab

Source: https://github.com/almali-dev/almali-library-curator/issues/29 (open when fetched). Exact issue text:

> `_services.html:42` has no `target="_blank"`, so the owner leaves the wizard to enter the code and must navigate back (it compounds the 3 s refresh issue). Open it in a new tab; `rel="noopener noreferrer"` is already there.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S9 — #17: ux: every flash message shows the warning icon, success included

Source: https://github.com/almali-dev/almali-library-curator/issues/17 (open when fetched). Exact issue text:

> `templates/base.html:56-58` picks `check` only for the `success` category, but `web/actions.py:83` calls `flash(key)` without a category, so "Kept forever", "Settings saved" and "Plex is connected" all show a warning icon. Owners learn to ignore warnings.
> 
> **Suggested fix:** pass `'success'` or `'error'` from `_result`/`_perform`, and use `role="alert"` for errors (see the related low issue).
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S10 — #22: ux: error flashes use role="status" instead of role="alert"

Source: https://github.com/almali-dev/almali-library-curator/issues/22 (open when fetched). Exact issue text:

> `templates/base.html:57` renders every flash with `role="status"`, so failures such as `web.flash.action_failed` are announced politely. Use `role="alert"` for the error category (pairs with the flash-icon issue).
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S11 — #16: ux: the pending Plex sign-in reloads the page every 3 s and wipes the manual form

Source: https://github.com/almali-dev/almali-library-curator/issues/16 (open when fetched). Exact issue text:

> `web/templates/web/_services.html:36` has `<meta http-equiv="refresh" content="3">` in the pending branch, while the manual address/token form stays on the same page by design (comment at lines 3-5). Any half-typed address or token is lost every 3 s, screen-reader users lose their place, and the refresh cannot be paused (WCAG 2.2.1). This is the required connection step of the first-run wizard.
> 
> **Suggested fix:** poll the sign-in status with a small script that updates only the status region, or refresh only through the existing "Check again" button.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S12 — #18: ux: the kill-switch badge reads "Removal failed" when on and "Protected" when off

Source: https://github.com/almali-dev/almali-library-curator/issues/18 (open when fetched). Exact issue text:

> `web/templates/web/settings.html:31` reuses `state_badge('removal_failed' if model.kill_switch else 'protected')`: a deliberate safe state looks like a red failure, and deletions-enabled looks like "Protected".
> 
> **Suggested fix:** dedicated `state.kill_switch_on` / `state.kill_switch_off` keys and badges in en, ca and es.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S13 — #19: ux: dry run and the kill switch are not shown on the Leaving soon tab

Source: https://github.com/almali-dev/almali-library-curator/issues/19 (open when fetched). Exact issue text:

> The dry-run note only renders for the Candidates and Protected tabs (`web/templates/web/library.html:44`), and the kill switch is shown nowhere outside Settings. On Leaving soon a fresh install (dry run by default) shows "Final check passed · awaiting deletion" although nothing will be deleted.
> 
> **Suggested fix:** show the dry-run / kill-switch badge on every Library tab and reword the re-check line while either is active.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S14 — #20: ux: "Resume deletions" and "Forget the Plex token" act without confirmation

Source: https://github.com/almali-dev/almali-library-curator/issues/20 (open when fetched). Exact issue text:

> `settings.html:33-34`: one click re-arms the deletion of items whose grace period has already passed. `_services.html:122-127`: one click disconnects Plex. Mark/undo/keep are fine without a confirmation (undo plus grace period), these two are not.
> 
> **Suggested fix:** a confirmation step for both (a confirm page or a second explicit button).
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S15 — #23: ux: the 503 page keeps the full navigation

Source: https://github.com/almali-dev/almali-library-curator/issues/23 (open when fetched). Exact issue text:

> `templates/error.html` does not override `sidebar_sections` like `login.html:7` does, so the 503 page lists Library, History, Connections and Settings, and every link answers the same 503. Suppress the navigation on error pages.
> 
> ---
> Found by the Palette pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S16 — #33: security: the installer accepts a user name with a line break

Source: https://github.com/almali-dev/almali-library-curator/issues/33 (open when fetched). Exact issue text:

> `install-unraid.sh:106-109` only checks that the user name is not empty, and `write_env_file` (`:398-412`) writes it raw into the Docker `--env-file`, so `ALC_INSTALL_USER=$'x\nPUID=0'` adds extra env lines. Only the owner can do this from the host shell, but the password is already checked for line breaks (`:118-121`); apply the same check to the user name and refresh `install-unraid.sh.sha256`.
> 
> ---
> Found by the Sentinel pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S17 — #30: security: /healthz shows the exact version before sign-in

Source: https://github.com/almali-dev/almali-library-curator/issues/30 (open when fetched). Exact issue text:

> `app.py:228-240` (public endpoint, `auth.py:31`) returns `version` and `schema_version` without a session, so anyone on the LAN can fingerprint the release. Keep `status`, `ready` and `missing` public and show the version only on the signed-in About page (S19). Update the health-check contract tests (L98 d pins only `status`).
> 
> ---
> Found by the Sentinel pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S18 — #31: security: settings.json and the database are created group-readable (0664)

Source: https://github.com/almali-dev/almali-library-curator/issues/31 (open when fetched). Exact issue text:

> `config.py:194-196` (`_write_settings_file` via `write_text`) and the SQLite database from `db.connect` use the default umask. Neither holds secrets, but library contents and watch history are readable by any user in the host group, unlike the 0600 key and token files (`secret_store.py:66-70`). Create both owner-only (mkstemp + `os.replace`, or an explicit chmod).
> 
> ---
> Found by the Sentinel pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S19 — #32: security: the image's Python dependencies are resolved at build time without hashes

Source: https://github.com/almali-dev/almali-library-curator/issues/32 (open when fetched). Exact issue text:

> `Dockerfile:9-13` builds wheels from the `pyproject.toml` floors capped by `docker/constraints.txt`. A compromised index or dependency release between the tag push and the build enters the signed image (cosign signs whatever was built). Commit a hashed lock file and build with `--require-hashes`; align the `pyproject.toml` floors (e.g. Gunicorn) with the image caps.
> 
> ---
> Found by the Sentinel pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S20 — #13: perf: Tautulli history is imported 25 rows per request

Source: https://github.com/almali-dev/almali-library-curator/issues/13 (open when fetched). Exact issue text:

> `tautulli/client.py:43, 190-201` uses `page_size=25` and `from_settings` does not raise it: 100k history rows mean 4,000 sequential requests during enrichment. Raise `length` (Tautulli accepts larger pages) to cut the request count 4-20 times.
> 
> ---
> Found by the Bolt pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S21 — #14: perf: file refresh issues one UPDATE per file

Source: https://github.com/almali-dev/almali-library-curator/issues/14 (open when fetched). Exact issue text:

> `enrichment/files.py:70-92`: about 23k indexed UPDATEs plus 23k `stat` calls per run. Acceptable, but one `executemany` would roughly halve it.
> 
> ---
> Found by the Bolt pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S22 — #11: perf: the scheduler checks today's slot by scanning every scheduled run

Source: https://github.com/almali-dev/almali-library-curator/issues/11 (open when fetched). Exact issue text:

> `scan/runner.py:150-163` reads all scheduled `scan_runs` and parses each timestamp in Python (`SCAN scan_runs`). Negligible at one row a day; add an index or `ORDER BY id DESC LIMIT 1` before the table grows.
> 
> ---
> Found by the Bolt pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S23 — #3: perf: the default Plex rate limit makes a full sync sleep about 7.5 minutes

Source: https://github.com/almali-dev/almali-library-curator/issues/3 (open when fetched). Exact issue text:

> A full sync of 5,000 movies and 300 series issues about 2,320 Plex requests: one per series and one per season (`plex/sync.py:80-92`), plus section and history pages. The default `plex_rate_limit_per_second = 5.0` (`config.py:89`, enforced in `plex/client.py:104-111`) turns that into **457 s of sleep**; the same sync without the throttle took 8.7 s on the fake server. This alone breaks the T15 target of a scan under 5 minutes.
> 
> **Suggested fix:** raise the default for a LAN Plex server (keeping retries and backoff), and/or replace the per-season `children()` calls with bulk reads (for example `/library/sections/{key}/all?type=4`). Expected: sync HTTP time from ~460 s to under 30 s.
> 
> ---
> Found by the Bolt pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

### S24 — #10: perf: a full sync runs 52,000 single-row SQL statements

Source: https://github.com/almali-dev/almali-library-curator/issues/10 (open when fetched). Exact issue text:

> Per-episode season lookups (`plex/sync.py:171-175`, 17,754), per-event user lookups (`:211-214`, 4,810) and 29,834 single-row upserts (`:139-161`). All hit indexes, so the wall cost is small today, but it is about three times the statements needed.
> 
> **Suggested fix:** prefetch the season-key and user maps once and batch writes with `executemany`.
> 
> ---
> Found by the Bolt pass on `45d401c` (feature document `odd/tasks/almali-library-curator.md`, Log L132).

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | v0.21.0 release: finish existing T16 review, then verified version/tag/publish | S1 | parent | release commit v0.21.0 `a8553a3` (correction `00a3954`, review-3c2ed519ade5b9ef approved); tag and push pending the clean suite | 2e48e1a; RED 5 failed; GREEN 214 passed; full suite 1308 passed/7 skipped; verifier mw85b2rf-e-9ogu failed without verdict; native validation/approval/publication pending |
| T2 | v0.22.0 quick UI fixes | S2,S3,S4,S5,S6,S7,S8 | worker | release commit v0.22.0 `48cbcda` (port `d31fb6d`); tag and push pending the clean suite | ff00203; RED 10 failed → GREEN 12 passed; browser/regression 141 passed, no skips; parent spot 12 passed; native medium, reviewDue=false; integrated 9f3d371; seam suite 1326 passed/7 skipped; release pending |
| T3 | v0.23.0 flash success/error semantics | S9,S10 | worker | release commit v0.23.0 `1344df4` (port `6e9bc85`); tag and push pending the clean suite | 0e1fb8b; RED 6 failed → GREEN 6 passed; regression 291 passed; parent spot 6 passed; native medium, reviewDue=false; integrated d5929be; seam suite 1326 passed/7 skipped; release pending |
| T4 | v0.24.0 Plex pending refresh, safety indicators, confirmations, 503 navigation | S11,S12,S13,S14,S15 | parent/worker | release commit v0.24.0 `33e4e81` (port `55c748e`); tag and push pending the clean suite | caf6a9f on feature/issues-v024 (integrated 88514d7); worker finished the partial diff: RED 16 failed (focused 42) → GREEN 201 focused, 803 web/ui |
| T5 | v0.25.0 installer username validation | S16 | parent | release commit v0.25.0 `e071654` (port `ed2480d`); tag and push pending the clean suite | 70059bd on feature/issues-v025-input-health; RED 4 failed → GREEN 4 passed; packaging 107 passed/7 skipped; independent verifier mw87kwe3-h-aqbg PASS (59 passed); native review (high, reviewDue) and integration pending |
| T6 | v0.25.0 public health information minimization | S17 | parent | release commit v0.25.0 `e071654` (port `d5292a6`); tag and push pending the clean suite | d421865 on feature/issues-v025-health; RED 4 failed → GREEN 51 passed (health, privacy, about, docker probe); assess/verification of security tier and integration pending |
| T7 | v0.25.0 owner-only settings/database; close release | S18 | parent (finished the failed worker's diff) | release commit v0.25.0 `e071654` (port `c9e858d`, correction `e071654`, review-a2b3be0b1a795ce4 approved); tag and push pending the clean suite | 0187174 on feature/issues-v025-privacy; RED 2 failed → GREEN 2 passed; parent fix: chmod is best effort for a file owned by another user (RED 1 failed → GREEN, 57 passed on db/settings/secret_store); full suite job-1 pending |
| T8 | v0.26.0 hashed image dependencies; close release | S19 | worker | release commit v0.26.0 `e6c05ac` (port `a2fe884`); tag and push pending the clean suite | 1afee28 on feature/issues-v026-hashes; worker RED 9 failed → GREEN 114 packaging passed; independent verifier mw9o9aou-6-8j1m PASS 8/8 (both arch downloads exit 0, tamper refused, lock reproducible); docker build unproven (no daemon) |
| T9 | v0.27.0 Tautulli page sizes | S20 | parent | release commit v0.27.0 `745ed5b` (port `cb527bb`); tag and push pending the clean suite | 4a5a9e6 on feature/issues-v027-perf; worker mw9nfzu0-4-v2ig RED→GREEN; related suites 79 passed |
| T10 | v0.27.0 batched file refresh | S21 | parent | release commit v0.27.0 `745ed5b` (port `b2ba876`); tag and push pending the clean suite | 4409eb9 on feature/issues-v027-perf; worker mw9nfzu0-4-v2ig RED→GREEN; related suites 79 passed |
| T11 | v0.27.0 scheduler lookup; close release | S22 | parent | release commit v0.27.0 `745ed5b` (port `7e3435f`); tag and push pending the clean suite | dbd3b2b on feature/issues-v027-perf; worker mw9nfzu0-4-v2ig RED→GREEN; related suites 79 passed |
| T12 | v0.28.0 Plex sync request/throttle improvements | S23 | worker | release commit v0.28.0 `6b72138` (port `0ab8e65`); tag and push pending the clean suite | verifier mw9p3vwu-8-kxaf PASS 6/6 (identity over three syncs and edge cases, fallback, transport, rate-limit precedence, rollback, 358 passed); real-Plex includeGuids on bulk reads unverified; 24f564d on feature/issues-v028-plex (integrated 3714b12; one commit, sync.py shared); RED 3+1 failed → GREEN 96 plex, 358 related, 776 web/ui; identity snapshot green on base and new |
| T13 | v0.28.0 sync SQL batching; close release | S24 | worker | release commit v0.28.0 `6b72138` (port `0ab8e65`); tag and push pending the clean suite | verifier mw9p3vwu-8-kxaf PASS 6/6 (identity over three syncs and edge cases, fallback, transport, rate-limit precedence, rollback, 358 passed); real-Plex includeGuids on bulk reads unverified; 24f564d on feature/issues-v028-plex (integrated 3714b12; one commit, sync.py shared); RED 3+1 failed → GREEN 96 plex, 358 related, 776 web/ui; identity snapshot green on base and new |

## Log

### L1 — Original owner request

En quant a les Issues, fes primerr les més ràpides. Si cal fes mes versions abans d'arribar a la versió 1.0. Crea un Roadmap de versions i les revisem.

### L2 — Approved execution boundary

Continua fins al v0.28.0

### L3 — Baseline and constraints

Base f2ed968; main worktree remains unchanged while native review review-65921fd6557d174c is bound to target sha256:f4cb0c53eb2b0066be361a9dbfd3326c439f110563aaa8046f29f7064a9c7c9c. Existing evidence: full suite 1303 passed/7 skipped; independent verifier PASS four T16 follow-ups. Reliability captures terminated repeatedly; no approval inferred. Implement independent later units in isolated worktrees; do not publish them out of order or change native authority. No meaningful RED for this tracking document; structural validation applies.

### L4 — Native review and parallel execution

Reliability capture succeeded; native refuter requires one bounded correction for signature/tag identity, shell example continuations, stopped-container health acceptance and service-key preservation. Correction plan admitted: 200 diff lines. RED job-7: five failed, 209 deselected, exit 1. GREEN/regression job-8 running. Main worktree contains only bounded correction files; no new version/tag. T2 worker mw84vnwz-c-9m2k in .review/issues-v022; T3 worker mw84vo0a-d-sx8k in .review/issues-v023. Tracking commit 5242cd8 is documentation-only; earlier branch history is inherited and is not a new review/PR slice. docs/ROADMAP.md updated in .review/pre-028 to reflect approved scope.

### L5 — Verified independent work units

T2 ff00203 and T3 0e1fb8b are committed only on their isolated worker branches. Both worker checks and parent focused spot checks passed. Each native assessment returned medium and reviewDue=false; no separate native review started. T2/T3 remain in_progress until integration/full-suite and sequential milestone publication. Risk: none for these requested UI-only outcomes; no unrequested contract changes intended. Main T16 correction GREEN/regression job-8 still pending.

### L6 — Correction checks complete; independent proof unavailable

Main worktree bounded correction committed as 2e48e1a (6 files, +89/-20, 109 diff lines within admitted 200). job-8 exit0: 214 passed in105.83s. job-9 exit0: 1308 passed, 7 skipped in557.56s. Independent verifier mw85b2rf-e-9ogu failed with "assistant reported an error" and returned no verdict; subagent_result confirmed no additional evidence. Do not retry unavailable verification unchanged or count prior T16 verification as covering this new correction. Native inspect sees dirty correction as a separate current-changes target 6d9cb40c1157fd4b47c2f55b68207cdefd9f45b1ee8b8b2d7abc2a7e01cecc17 and offers another START; no new lineage started because this correction belongs to open review-65921fd6557d174c. After restoring independent proof, resume that original lineage with committed correction, not a duplicate review. T2/T3 implementation checks/commits exist; seam integration and releases remain pending. No v0.21.0+ tags or pushes. Needs owner decision: restore a working gentle-ai-verify configuration and authorize a bounded rerun, or provide independent human verification before native targeted validation/publication. Risk: item 2 (signature execution identity and credential persistence); item 5 (installer delivery).

### L7 — Explicit owner stop

Owner instruction verbatim: "Guarda i tanco". Work is intentionally paused with T1 blocked on unavailable independent proof and T2/T3 implementations committed but not integrated/released. T4–T13 remain pending under the existing authorization; do not launch more work while the owner closes this session. Full recovery summary saved to Engram observation 4130. No tag, push, review waiver, duplicate lineage or further verifier attempt authorized by this stop.

### L8 — Automatic execution resumed

Owner instruction verbatim: "Continua. Estàs ara en mode automàtic en principi fins la v0.28.0". Pause lifted for authorized implementation. Publication remains gated by missing independent T16 proof; no unchanged verifier retry or native duplicate review. Integrated correction/UI units into tracker branch as 9993443,9f3d371,d5929be; the only cherry-pick conflict was additive Unreleased changelog notes and both notes were retained. Original commits/branches preserved. Seam full suite job-10 running on d5929be; no behavior changes in this worktree until it finishes. T4 UX and T7 file privacy will run as independent bounded workers in separate worktrees.

### L9 — Independent writers launched

T4 worker mw87eml2-f-9xk8 runs in registered .review/issues-v024; T7 worker mw87emql-g-m0z6 in registered .review/issues-v025-privacy. Both use baseline0d4e205 and narrow allowed surfaces. Neither unit is verified or done yet. Seam suite job-10 still running; T1 still blocked on missing independent proof. No releases have advanced.

### L10 — Inline installer input unit

T5 starts test-first in registered .review/issues-v025-input-health, branch feature/issues-v025-input-health, base0d4e205. Bounded reads confirm owner-name validator checks only emptiness, whereas password already rejects LF/CR. T6 health payload currently includes version/schema_version and remains pending until its own RED step. T4/T7 workers and job-10 continue; none is marked done.

### L11 — Installer rule green; remaining independent units

T5 public installer RED4 exit1 → GREEN4 exit0; packaging job-11 exit0:107passed7skipped in10.23s; bash syntax/shellcheck/checksum/diff-check pass. Independent T5-only verifier mw87kwe3-h-aqbg active; no verdict yet and T5 not done. T6 begins its own RED in a separate worktree, not the verifier target. T8 hash-lock and T12/T13 Plex sync are independent several-minute behavior units and will launch together on isolated worktrees; no file-type split or main publication.

### L12 — Explicit owner stop after worker incident

Owner instruction verbatim: "guarda i tanco". Evidence since L11: seam suite job-10 on tracker d5929be exit 0, 1326 passed, 7 skipped in 561.44s. T5 committed 70059bd (independent verifier mw87kwe3-h-aqbg PASS); T6 committed d421865 (RED 4 failed → GREEN 51 passed). Neither is integrated into the tracker, native-reviewed or released.

Incident: four workers failed back-to-back with "assistant reported an error" and no report (T4 mw87eml2-f-9xk8, T7 mw87emql-g-m0z6, T8 mw87oqx5-i-j3p6, T12/T13 mw87or5m-j-vclj), the same symptom as the earlier T16 verifier mw85b2rf-e-9ogu; the T5 verifier between them succeeded, so the failure is intermittent in the subagent runtime, not in the code. Their partial diffs stay uncommitted in their worktrees and are unverified. Diagnose the runtime/profile before relaunching; do not relaunch unchanged.

Resume order: (1) inspect partial diffs in issues-v024 / issues-v025-privacy / issues-v026-hashes and decide finish-inline vs relaunch; (2) independent proof for main 2e48e1a, then the open native lineage review-65921fd6557d174c; (3) native review for T5/T6 (high risk); (4) sequential releases v0.21.0 → v0.28.0. No tags, pushes or releases happened.

### L13 — Session resumed in automatic mode with delegation

Owner instruction verbatim: "Continua. Activo mode yolo i delega a subagents." Subagent runtime probe: the T16 verifier mw9ncdkp-1-s41g on 2e48e1a launched (a first launch was refused because the task lacked the canonical `Risk: item N` line; the risk was already recorded as items 2 and 5). T7: the crashed worker's diff was complete; parent observed RED 2 failed → GREEN 2 passed, added a best-effort chmod so a database owned by another user still opens (RED 1 → GREEN), committed 0187174; full suite job-1 running. Four workers launched together on disjoint worktrees: T4 mw9nfz86-2-2ojg (.review/issues-v024, finishes the partial diff), T8 mw9nfzlp-3-eb49 (.review/issues-v026-hashes), T9–T11 mw9nfzu0-4-v2ig (new .review/issues-v027-perf from f5ede34), T12–T13 mw9ng089-5-5nwe (.review/issues-v028-plex). Workers do not edit CHANGELOG or odd/ docs; the parent integrates and releases in order.

### L14 — T1 review restarted; T9–T11 committed

Independent verifier mw9ncdkp-1-s41g on 2e48e1a: **PASS** on all four correction items (digest-bound signature and run, single-backslash continuations, exited container never healthy, omitted key never replaces a concurrent key); 214 passed; checksum OK; shellcheck info-only findings pre-existing at f2ed968. The open lineage review-65921fd6557d174c could not continue through the facade (facade STATUS falls back to current-changes; capture rejected, no mutation). Owner choice verbatim: "Nova revisió del codi corregit (recomanat)". It was abandoned through the package CLI (operator_disposition, quarantined) and a fresh native review **review-3c2ed519ade5b9ef** started over 510119c..2e48e1a (tier high, 24 paths, 1,936 lines, lenses risk/resilience/reliability, consent resolved by the host). The first group capture failed with no mutation: review-resilience (nan/deepseek-v4-flash) spent its 16,384 output tokens on reasoning and returned no text (stopReason length), the same failure as L139. T9–T11 worker mw9nfzu0-4-v2ig delivered; committed 4a5a9e6 (S20), 4409eb9 (S21), dbd3b2b (S22, migration 006) on feature/issues-v027-perf.

### L15 — T8 verified; seam suite; reviewer profiles

Seam suite job-2 on 671a917 (T2, T3, T5, T6, T7): exit 0, 1337 passed, 7 skipped. T9–T11 integrated as 9987571, 166b179, a5a0ecc (82 related passed). T8 committed 1afee28; independent verifier mw9o9aou-6-8j1m **PASS** on all eight checks. Parent recommendation for the reviewer profiles (owner asked): review-resilience (nan/deepseek-v4-flash, 16,384-token output cap) at medium, review-reliability (nan/glm5.3-flash, 32,768) at high, review-readability at medium, review-risk unchanged. After the owner reported the change, `~/.pi/agent/subagents.json` still read `max` for the three nan profiles, so the reviewer group was not relaunched.

### L16 — T1 review findings and bounded correction; T4, T12–T13 delivered

Owner (verbatim): "Ja he fet els canvis. Provem i seguim"; the profile file still read `max`, and the owner chose "Sí, edita'l (recomanat)": the parent set review-resilience medium, review-reliability high, review-readability medium in ~/.pi/agent/subagents.json (backup in /tmp). All three reviewers then returned. The refuter corroborated four candidate-caused CRITICAL findings and review-3c2ed519ade5b9ef moved to correction_required: R3-live-failure-eviction / R4-GUARD-EVICTION-RESET (a full guard table evicts live failure state, so the lockout can be bypassed), R4-INSTALLER-WAIT-UNBOUNDED (no wall-clock bound, docker inspect without timeout), R3-pulled-digest-alias (RepoDigests[0] may be another repository); plus WARNING R3-zero-poll-interval. Correction plan admitted: 190 lines (budget 200). Correction worker mw9p3vrq-7-phcd in the main worktree (hard cap 185 lines); the guard fix refuses new untracked addresses with 429 when every slot holds live failure state, extending the L144 owner choice. T4 committed caf6a9f; T12–T13 committed 24f564d as one commit (the worker's split shares sync.py hunks); independent verifier mw9p3vwu-8-kxaf (item 6). T4, T8, T12–T13 integrated into the tracker (88514d7, 2ef87c1, 3714b12); seam suite job-3 running.

### L17 — Releases v0.21.0–v0.28.0 committed on the release branch

Owner (verbatim): "D'acord. Seguim el pla." Delivery strategy: the integrated units were ported in version order onto `feature/almali-library-curator-v1` (cherry-pick -x; only additive CHANGELOG conflicts and one regenerated installer checksum), each followed by its own `chore: release` commit. Native assess per release range: v0.22.0, v0.23.0, v0.24.0, v0.26.0, v0.27.0, v0.28.0 medium with `reviewDue: false`; v0.25.0 high → review-a2b3be0b1a795ce4 (33e4e81..c91b3f1, lenses risk/reliability) found two corroborated CRITICAL regressions in c9e858d — settings saved through a symlink replaced the link (R3-001) and an existing read-only database could no longer be opened (R3-002); bounded correction `e071654` (worker mw9qqr62-9-dc5r, RED 2 → GREEN 59, 95 lines within the 110-line plan), targeted validation passed, approved and acknowledged. Each release gets a full suite in its own clean worktree (`.review/rel-0XX0`); v0.22.0 1322 passed + 1 pre-existing flaky disk-usage test (`tests/hostfs/test_filesystem.py::TestDiskUsage::test_reports_consistent_usage_math_for_a_real_root` reads the real disk twice; 25/25 on rerun; follow-up), v0.23.0 1329 passed, v0.24.0 1353 passed. An earlier v0.21.0 suite (job-4) was invalidated because the parent changed its worktree mid-run. Tags are pushed in order, one publish run at a time, so `latest` ends on v0.28.0.
