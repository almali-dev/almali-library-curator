# 0.33 Public-ready — deletion safety, migration safety, release pipeline, hygiene

Milestone 0.33 of the pre-1.0 audit of v0.32.0 (audit: `odd/audits/pre-1.0-audit-v0.32.0.md`).
Branch: `feature/almali-library-curator-v1`. Delivery: work units committed on the branch, each
reviewed and verified before it closes; the milestone ends with the version bump to `v0.33.0`.

## Specs

The milestone content, verbatim from the audit:

> 0.33 Public-ready | Deletion-safety guards, migration safety, release pipeline on `main`, hygiene

Each `S#` is one GitHub issue of milestone `0.33 Public-ready`. The issue body is the requirement
source; the quoted text below is verbatim.

### S1 — #38 deletion through Radarr/Sonarr does not verify that the target owns the marked files

Verbatim from the issue:

> Before any Radarr/Sonarr mutation the app confirms that every file in the marked scope belongs to that arr resource (path under the resource path after the path map, and matching tmdb/tvdb id). Otherwise it fails with a fixed code and sends nothing.

> Linking uses tmdb/tvdb/imdb id or exact title+year only (`enrichment/linking.py:66-85`). `_arr_delete` trusts the stored id and only checks `tmdbId is None` (`lifecycle/deletion.py:357-400`); `RadarrMovie.path` exists (`radarr/models.py:33`) but is never compared with `media_items.file_path`. Season deletes select Sonarr files by `seasonNumber` (`deletion.py:383-388`) and `SonarrEpisodeFile` has no `path`. A failed link step keeps old ids (`linking.py:128-130`).

> Suggested fix: re-GET the resource before mutating, require the external id to match the Plex item, delete only episode files whose mapped path is in the scope set, and refuse when the sets differ.

> Severity: Blocker for 1.0. Found by the pre-1.0 audit of v0.32.0 (2026-11-12).

### S2 — #39 a request is recorded as deleted when the files were never visible

Verbatim from the issue:

> "Deleted" is reported only after files that were seen on disk before the call are confirmed gone, as SECURITY.md states. With no readable media root the request fails with a code such as `files_unknown`.

> A failed pre-delete `stat` is ignored (`lifecycle/deletion.py:160-165`), `FileNotFoundError` after the call counts as gone (`:259-266`), and `RadarrNotFound`/`SonarrNotFound` return silently (`:366-367`, `:381-382`, `:392-393`). `HostFilesystem.is_mounted` is used only in `web/views.py:315`. The request becomes `deleted` with 0 bytes and a history row while the files still exist.

> `tests/lifecycle/test_deletion.py:217-229` and `:454-469` pin this as intended behaviour.

> Suggested fix: require at least one successful pre-stat and a readable media root before claiming; allow "already gone" only on a retry that saw the files earlier.

> Severity: Blocker for 1.0. Found by the pre-1.0 audit of v0.32.0 (2026-11-12).

### S3 — #49 a failed Tautulli import or arr linking does not block deletion

Verbatim from the issue:

> When a configured service's history import or linking fails, deletion is skipped for that run.

> The dependency list omits enrichment (`scan/runner.py:126-129`), and `tests/scan/test_runner.py:194-209` asserts that a `history` error must not stop deletion. The final check sees no play and the movie is deleted.

> Suggested fix: treat `import_tautulli_history` and `link_arr.*` errors as deletion blockers when those services are configured.

> Severity: High. Found by the pre-1.0 audit of v0.32.0 (2026-11-12).

### S4 — #40 schema migrations are not atomic and a newer schema opens silently

Verbatim from the audit: "Schema migrations are not atomic and a newer schema opens silently" (Reproduced).

### S5 — #50 items Plex no longer has are never pruned

Verbatim from the audit: "Items Plex no longer has are never pruned" (Read in code).

### S6 — #41 `main` lacks everything since v0.16.0; releases are published without CI

Verbatim from the audit: "`main` lacks everything since v0.16.0; releases are published without CI" (Confirmed).

### S7 — #44 internal task logs and personal identity would be published

Verbatim from the audit: "Internal task logs and personal identity would be published" (Confirmed).

### S8 — #55 publishing has no concurrency control, tag immutability or stable-only `latest`

Verbatim from the audit: "Publishing has no concurrency control, tag immutability or stable-only `latest`".

### S9 — #53 a failed `docker run` leaves an install neither script can recover

Verbatim from the audit: "A failed `docker run` leaves an install neither script can recover".

### S10 — #54 the documented upgrade command drops labels, mounts and signature verification

Verbatim from the audit: "The documented upgrade command drops labels, mounts and signature verification".

### S11 — #43 the Unraid template documents a path-map syntax the app ignores

Verbatim from the audit: "The Unraid template documents a path-map syntax the app ignores" (Confirmed).

### S12 — #57 `scan_runs.report` grows about 1.9 GB a year and is re-parsed on every page

Verbatim from the audit: "`scan_runs.report` grows about 1.9 GB a year and is re-parsed on every page" (measured).

### S13 — #59 a live scan holds the SQLite write lock for the whole Plex sync

Verbatim from the audit: "A live scan holds the SQLite write lock for the whole Plex sync" (measured).

### S14 — #61 a changed service address receives the stored credential

Verbatim from the audit: "A changed service address receives the stored credential".

### S15 — #62 no security headers; default session cookie name

Verbatim from the audit: "No security headers; default session cookie name".

### S16 — #66 several documented claims do not match the code

Verbatim from the audit: "Several documented claims do not match the code".

### S17 — #67 CI silently skips workflow and layout tests and has no lint or type gate

Verbatim from the audit: "CI silently skips workflow and layout tests and has no lint or type gate".

### S18 — #68 no migration-chain test and no HTTP end-to-end deletion cycle

Verbatim from the audit: "No migration-chain test and no HTTP end-to-end deletion cycle".

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | The arr target owns the marked files before any mutation, and "deleted" is never claimed for files that were never seen | S1, S2 | worker | done | `5f12a0a`, `f74321f`, `178abf0` after four rounds and three independent reviews; one open item: the dry-run preview still diverges from the real run on the Plex route (L8) |
| T2 | A failed Tautulli import or arr linking blocks deletion for that run | S3 | worker | done | `01e9e0a`; worker `mwe2qzro-2-pr64` plus the parent correction (a raised enrichment step also blocks the run) |
| T3 | Migrations are atomic and a newer schema refuses to open | S4 | worker | done | `d67ac5a`; worker `mwe2wrdz-3-pkod` with the follow-ups `mwe2zu2h-4-a4hf` and `mwe32rdo-5-8sz2`, plus the parent correction (same-second backup names) |
| T4 | Items Plex no longer has are pruned | S5 | worker | in review | `17c96d7` on `wip/033-t4-prune` (worker `mwf4es0t-6-5v8w`); verifier `mwf5mngn-f-w1g3` running; Risk: item 1, item 3 |
| T5 | Releases are cut from `main` with CI, and publishing is concurrent-safe, immutable and stable-only `latest` | S6, S8 | worker | in review | `b140244` on `wip/033-t5-release`; merge to `main`, branch protection, cancelling the queued run and GitHub Releases stay owner actions; verifier `mwf5mn38-c-ojg4` running; Risk: item 5 |
| T6 | The published repository carries no internal task log or personal identity | S7 | worker | pending | Risk: item 3 |
| T7 | A failed run leaves a recoverable install; the upgrade command keeps labels, mounts and signature verification; the template path map matches the app | S9, S10, S11 | worker | in review | `35ad3cc` on `wip/033-t7-installer`; verifier `mwf5mn72-d-8dvn` running; Risk: item 5 |
| T8 | The scan report does not grow without bound and the write lock is not held for the whole Plex sync | S12, S13 | worker | in progress | worker `mwf5mnmd-g-ilhq` on `wip/033-t8-perf` (based on T4); Risk: item 4, item 3 |
| T9 | Security headers, a named session cookie, and no credential for a changed address | S14, S15 | worker | in review | `3a2738d` on `wip/033-t9-web`; the cookie rename signs every owner out once; verifier `mwf5mnag-e-yk47` running; Risk: item 2 |
| T10 | Documented claims match the code; CI runs the skipped tests and gates lint and types; a migration-chain test and an HTTP end-to-end deletion cycle exist | S16, S17, S18 | worker | pending | Risk: item 6 |
| T11 | The dry-run preview tells the same outcome as the real run on the Plex route (multi-version item, item Plex no longer knows) | S1, S2 | worker | in review | `fbaf5ab` on `wip/033-t11-parity`; verifier `mwf5mmus-b-h7lr` running; blocks v0.32.1 |

## Log

### L1 — Original request (the exhaustive audit)

Owner, verbatim:

> Fes una revisió exhaustiva del tot el projecte desde totes les vessants, ui, performance, security... revisa tots els processos, el d'instal·lació, el docker, els processos de puntuació, el moviment d...

### L2 — Milestones, issues and the plan of action

Owner, verbatim:

> Crea tot el que que calgui, milestones, issues, ... . Guarda l'informe en local. Seguim pel pla d'acció

The audit report was saved at `odd/audits/pre-1.0-audit-v0.32.0.md` (commit `ef3a502`). Milestones
0.33 Public-ready, 0.34 Field test, 1.0.0-rc, 1.0.0 and 1.1 were created on GitHub with the 38
issues #37–#74 distributed over them.

### L3 — Every finding becomes an issue of a later version

Owner, verbatim:

> Totes com a issues i ja les afegirem en una nova versió

Milestone 0.33 is that version for the 18 issues above; 0.34 carries the field test and the
contract fixes that need a live server.

### L4 — Resume

Owner, verbatim:

> Per on continuem?

The Unraid installation checkpoint (`odd/tasks/unraid-v032-install.md`, T2) is blocked on the
owner's private GHCR login on Tower, verified on 2026-11-12 by read-only SSH: the temporary
tooling (`cosign`, `install-unraid.sh`, `install-unraid.sh.sha256`) is present, but there is no
isolated Docker configuration, no Curator image, no container, no appdata and no listener on
17540. 0.33 is therefore started now, in parallel with that installation, which continues as soon
as the owner completes the private login.

### L5 — T2 delivered

Worker `mwe2qzro-2-pr64` implemented S3 test-first: RED 2 failed (both new contract tests),
GREEN 10 passed in `tests/scan/test_runner.py`. It rewrote the test that pinned the wrong
behaviour and added two contract tests plus a negative one that keeps unrelated enrichment
diagnostics from starving deletion. The runner now skips the deletion pass when the enrichment
step failed on a recorded `import_tautulli_history` or `link_arr.*` diagnostic; the production
diagnostic keys come from `enrichment/runner.py` (`link_arr.<name>`, `link_arr`,
`import_tautulli_history`), so the check fires on a real run.

Parent review found one gap: the check read only the recorded diagnostics, so an enrichment step
that **raised** (no diagnostic recorded) still let the deletion run on an unknown history. The
parent corrected it test-first (RED 2 failed: `test_an_enrichment_step_that_raised_blocks_deletion`
and the rewritten contract assertion; GREEN 11 passed in `tests/scan/test_runner.py`, 22 in
`tests/scan/`) and committed the unit as `01e9e0a`.

Decision recorded: a raised enrichment step blocks the run's deletion, because the failing
operation is unknown and an unknown history must not drive a deletion. Other diagnostics (a file
refresh that could not read a disk) stay non-blocking; the deletion executor still refuses to
claim a file it could not see before the call (S2, T1).

### L6 — T3 delivered

Worker `mwe2wrdz-3-pkod` implemented S4 in two steps: RED 2 -> GREEN 11 for the atomic migration
and the newer-schema refusal, then RED 4 -> GREEN 18 after the parent review. The migration and
its `PRAGMA user_version` bump commit together inside one `BEGIN IMMEDIATE ... COMMIT` with a
rollback on any failure; the pre-migration copy lands in `<config dir>/backups` (the config
directory is the parent of `<config_dir>/almali-library-curator.db`, so the container path is
`/config/backups`) through SQLite's online backup API, owner-only, failing closed, keeping the
five most recent; a database with a newer schema is refused before the file is written to.

The parent review sent two gaps back and the worker fixed both (`mwe2zu2h-4-a4hf`,
`mwe32rdo-5-8sz2`): the copy was taken on every startup even with no pending migration (the
condition is now `0 < user_version < SCHEMA_VERSION`), and the copy file was created with the
process umask before being chmod'ed, leaving a window where the host group could read it (it is
now created with `os.open(..., 0o600)` from the first byte).

The parent added the last correction: two copies in the same second collided on the name and the
second one failed the migration closed; `_create_backup_file` now gives the second copy its own
name. Committed as `d67ac5a`.

Decision recorded: the copy exists to protect a migration, so it is taken only when a migration
is actually pending; an up-to-date database is never copied on startup.

### L7 — Independent verification of T2 and T3

Verifier `mwe35mrd-6-4do5` worked in a detached worktree at `e549208` (`.review/verify-033`), so
the in-flight T1 changes in the main tree never touched the result. Verdict: **PASS on every rule
of S3 and S4**, with 34 checks for S3 (both enrichment failure shapes block the deletion pass,
unrelated diagnostics do not, the production diagnostic keys block, no step is over-blocked) and
39 checks for S4 (a failing migration rolls back its schema and its version together, the database
stays openable, a newer schema is refused with the file untouched and its journal mode kept, the
copy exists before a pending migration with the pre-migration content, it is owner-only with no
readable window, it fails closed, it never happens without a pending migration, and five copies
are kept).

Observations, none blocking, recorded as later work:

- A database at `user_version = 0` that already holds tables gets no copy. The app cannot produce
  that shape (every created database migrates in one pass) and the migration would fail anyway,
  but the literal wording of the rule is not met for it.
- The fail-closed path surfaces the underlying `FileExistsError` instead of a migration-specific
  error. The behaviour is right, the message is generic.
- Two copies in the same second sort as `...Z-1.db` before `...Z.db`, so the retention order is
  not strictly chronological in that case. Fresh copies always survive the pruning.
- `SchemaVersionError` reaches the user as a startup traceback rather than a friendly page. Not
  in the requirement, and the app refuses to start as it must.

### L8 — Checkpoint: the owner stops the session

Owner, verbatim:

> guarda i tanco

State at the stop, on `a8acad1`, 22 local commits ahead of the remote, nothing pushed:

- T1, T2 and T3 are committed. T1 needed four rounds: the reviews found a false refusal for items
  with no stored external id, a media-root gate that refused every deletion on a host without
  `/media`, scope-level instead of per-file retry evidence, a first attempt that claimed a file it
  never saw (closed with the strict per-file rule, a recorded parent decision), a `/` resource path
  that owned every file, an evidence commit that could strand a request in `deleting`, and wording
  that recommended the very bypass the guard prevents.
- The full suite is green: 1,625 passed, 10 skipped, exit 0. The eleven failures seen after the
  Plex contract fix were fixture faults, not product regressions (`a8acad1`).
- **Open, blocking the release:** the dry-run preview still promises `would_delete` on the Plex
  route where the real run refuses (a multi-version item: `plex_multi_version`; an item Plex no
  longer knows: `files_present`). The worker for it was cancelled at the stop before it wrote
  anything; the unit restarts from the committed baseline.
- **Open, blocking the release:** the native review. The provider refuses the whole range for
  context budget with four lenses and with one, so it must run as chained small candidates. Slice A
  (the scan runner and the migrations) is lineage `review-d09f7a31661fe761` in the worktree
  `.review/cand-a`, state `reviewing`; its `review-resilience` reviewer model (`zai/glm-5.3`)
  spent its whole budget on reasoning and returned no text twice, so the owner must switch that
  model or lower its thinking level before the slot is captured. Slices B, C and D follow.
- T4 to T10 are not started.

### L9 — Resume: everything that can run in parallel

Owner, verbatim:

> Fes tot el que puguis en paral·lel. En principi tots el models funcionen.

Plan of the parent: the native review of slice A (`review-d09f7a31661fe761`) resumes with the
configured reviewer models. In parallel, one worker per independent unit runs in its own linked
worktree under `.worktrees/` on a `wip/033-*` branch cut from this commit, so no worker sees
another's edits and the release branch only receives reviewed units: T11 (dry-run parity, needed
for v0.32.1), T4 (prune), T5 (release pipeline files only), T7 (installer, upgrade, template) and
T9 (headers, cookie, credential on a changed address). T8 waits for T4 (both change the Plex sync
and the schema), T10 waits for the rest (its migration-chain test needs the final schema and its
CI gate shares `ci.yml` with T5). T6 is split: the tree hygiene (ignored tool directories,
RFC 5737 fixture addresses, the stale PDF) can be done; publishing from a fresh root or rewriting
history, deleting branches, merging to `main` and cancelling the queued publish run stay owner
decisions.

### L10 — Native review of the committed range, slice by slice

The range `ef3a502..a8acad1` was reviewed as four chained committed candidates, each with three
lenses, from detached worktrees under `.review/`:

- **A** (`ef3a502..6a16a7e`, runner and migrations), `review-d09f7a31661fe761`: one critical
  finding, `migrate()` committed the caller's open transaction even with nothing pending. Parent
  correction test-first (RED 2, GREEN 42), `0da9152` on the candidate, `dc07f64` on the branch.
  **Approved and acknowledged.** Advisory: incomplete backups kept, retention order, backup
  resilience (R3/R4 warnings).
- **B** (`6a16a7e..f74321f`, deletion executor), `review-9d0d1efff8126ae3`: six critical findings
  (dry-run preview aborted by an arr failure, pathless Sonarr file ignored, pre-delete evidence
  written after the mutation, bookkeeping commit outside its guard, no fix-forward for a partial
  season deletion). Worker `mwf4sv25-9-xmsy` corrected all six test-first in 167 lines (RED 10,
  GREEN 221), `1219fcb` on the candidate, `cadb572` on the branch (the conflict with the later
  dry-run commit kept the later code and widened its handler to every exception). **Approved and
  acknowledged.**
- **C** (`f74321f..3b217d3`, Plex contract), `review-427441b3a58703ac`: one critical finding, the
  multi-version guard counted files instead of versions, so a single-version stacked item (CD1/CD2)
  was refused. Parent decision: a stacked single version is deleted only when every part passes the
  per-file rule; a part that cannot be proven refuses with `files_unknown`. Worker
  `mwf55r8o-a-ypos` corrected it in 139 lines (RED 10, GREEN 261), `2729035` on the candidate.
  **Blocked:** the targeted validator (`nan/glm5.3-flash`) produced a result the provider refused
  twice ("targeted validation result does not bind the provider-owned correction request"); the one
  allowed relaunch is spent. The correction is not on the branch yet.
- **D** (`3b217d3..a8acad1`, first-run flow and scan outcome), `review-dddfc2c11d0aa1f0`: one
  critical finding, the failed-scan text promised that nothing was deleted. Parent correction
  test-first (RED 3, GREEN 68), `69cc276` on the candidate, `ec03a51` on the branch. **Approved and
  acknowledged.** Eleven advisory warnings recorded by the provider for later work.

The first two independent verifiers (T11, T5) failed with a network error before reporting; they
were relaunched once together with the verifiers of T4, T7 and T9.


### L11 — The accumulated range stays unreviewed as one candidate

The stop hook offered a native START for the whole range `4e3bf7c..8bb836a` (27 commits, about
3,200 lines), which the provider had already refused for context budget. Owner, verbatim choice:

> Deixar-lo sense revisar com a rang (recomanat)

The range is covered by slices A, B and D (approved) and C (blocked on the validator, L10). The
full suite on `8bb836a` is green: 1,646 passed, 10 skipped, exit 0.

### L12 — Units verified, reviewed and integrated

Owner, verbatim: "alguna cosa en paral·lel?"

Each unit ran its own independent verifier and its own committed-range native review from its
`wip/033-*` worktree, then was cherry-picked onto the branch:

- **T11** `fbaf5ab` -> `9da4a0b`: verifier PASS; review `review-d8bb3c6fb0b2ed37` approved.
- **T5** `b140244`+`e329b43` -> `05245dd`+`7502d71`: verifier PASS; review `review-b16345f72ee0a8d8`
  approved after one correction (prerelease tag spelling in the test, branch dispatches isolated
  from the release concurrency group, sign-only repair of an unsigned existing version).
- **T4** `17c96d7`+`508f593` -> cherry-picked: verifier PASS; review `review-c08d350e6b4c5ef5`
  approved after one correction (a present season container keeps its request open).
- **T9** `3a2738d`+`129c3a7`+`8b46c6a` -> `3e1fd42`+`1786220`+`45dba47`: verifier found one test
  harness regression under the new CSP (fixed); review `review-35e372420d3154f6` approved after
  one correction (the Plex picker no longer blocks a blank token in the browser; the server still
  refuses it at a changed origin).

Escalated, not integrated yet:

- **T7** `35ad3cc`: verifier PASS; review `review-4f4a359df5c57dec` escalated (unknown causality).
  Corroborated: the upgrade removes the old container before marking the manifest, and the
  pre-upgrade archive misses a database or key linked outside appdata. The follow-up worker failed
  on model quota before writing anything.
- **T8** `a709439` (on T4): verifier PASS; review `review-94558e8c2d48a9f9` escalated on one
  inconclusive finding (legacy reports without detection sections become JSON null rows). The
  follow-up worker failed on model quota before writing anything.
