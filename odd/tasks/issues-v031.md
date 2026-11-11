# v0.31.0 — low-priority performance and v0.30.0 follow-ups (#12, #15)

Authorized scope: issues #12 and #15 plus the three follow-ups recorded in `odd/tasks/issues-v030.md` L7/L9 and `odd/tasks/issues-v029.md` (test gap), released as v0.31.0 on `feature/almali-library-curator-v1`; previous ladder: `odd/tasks/issues-v030.md`.
Runner: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q`. Push, tag publication and the publish run follow the ladder strategy: push the branch and the tag, one publish run at a time, so `latest` ends on the newest release.

## Specs

### S1 — Owner authorization

> Vols que tiri endavant la v0.31.0? (parent proposal: v0.31.0 with #12, #15 and the three follow-ups, workers in parallel, the CSV first)

> Endavant. He canviat l'origen del model i l'esforç

### S2 — #12: perf: the sources panel runs a UNION scan of media items and episodes on every render

Source: https://github.com/almali-dev/almali-library-curator/issues/12. Exact issue text:

> `web/views.py:180-197`: 8 ms at about 23k rows, linear. Fine for v1; cache it with the scan report when libraries grow.

Current position: `sources_view` (`web/views.py:275`, the UNION at `:284` and `:290`).

### S3 — #15: perf: a dry-run scan copies the whole database into memory

Source: https://github.com/almali-dev/almali-library-curator/issues/15. Exact issue text:

> `scan/runner.py:132-138` uses `conn.backup(preview)`: 1-2 s at T15 scale, growing with the history tables. Consider a savepoint/rollback preview instead of a full copy.

Current position: `scan/runner.py:115-120`. Hard constraint (parent): a dry run never writes anything to the live database; if any step commits, a savepoint/rollback preview is only acceptable when that commit provably cannot reach the live file.

### S4 — CSV formula escaping misses the tab and carriage-return prefixes

Recorded follow-up, verbatim (`odd/tasks/issues-v030.md` L9): "the escaping of the CSV checks `lstrip()` before the prefixes, so the tab and carriage return prefixes never match — it predates this release and stays outside it."

Current position: `_csv_cell` (`web/routes.py:122-126`).

### S5 — The users cache lives as long as the scan

Recorded follow-up, verbatim (`odd/tasks/issues-v030.md` L9): "the users cache lives as long as the engine and not as long as the scan".

Current position: `DetectionEngine._users` (`detection/engine.py:208`, `_load_users` `:301-321`); the engine is built per scan in `scan/pipeline.py:51`.

### S6 — Test gap: a new play on an episode cancels a seasons request

Recorded follow-up, verbatim (`odd/tasks/issues-v029.md` L-status): "the re-check tests cover a movie without protector, and no test pins \"a new play on an episode cancels a seasons request\"."

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | Feature document, Engram mirror, todo | S1 | parent | done | this file |
| T2 | CSV escaping covers the tab and carriage-return prefixes | S4 | worker W-C `mwb4vs2w-3-fks6` | done | `a620220`; RED 4 CSV failures → GREEN 17 cases through `GET /history.csv` |
| T3 | The users cache is scoped to one scan | S5 | worker W-C `mwb4vs2w-3-fks6` | done | `bd2cdf6`; RED 24 reused-engine failures → GREEN 24; query budget test still 1 users query per pass |
| T4 | A test pins that a new episode play cancels a seasons request | S6 | worker W-C `mwb4vs2w-3-fks6` | done | `607e6ef`; test only, 6 cases, the behavior already held (no RED by design) |
| T5 | The sources panel no longer scans every row on each render | S2 | worker W-B `mwb4vs0m-2-0zv7` | done | `9e9e424`; RED 10 failed (renders still ran the UNION) → GREEN 27; tests/web, tests/enrichment, tests/scan 855 passed |
| T6 | The dry-run scan no longer copies the whole database, and still never writes to it | S3 | worker W-A `mwb4vrxp-1-8tz5` | dropped (unsafe, L3) | No change: a savepoint preview would commit to the live file, and neutralizing the commits would hold the live write lock for the whole scan (L3); #15 stays open with the evidence |
| T7 | Verification, version 0.31.0, CHANGELOG, roadmap, tag, publish, close #12 and #15 | S1-S6 | parent + independent verifier | done (L9) | release commit `f17d1a0`; inline verification (L8): focused 74 passed, native assess `reviewDue: false` (`under_budget`), no independent verifier required; annotated tag `v0.31.0` pushed, publish run `43059234759` success, #12 closed, #15 commented (L9) |

## Log

### L1 — Original owner request

> Endavant. He canviat l'origen del model i l'esforç

Context: the parent had proposed v0.31.0 with #12, #15 and the three follow-ups, delegated to parallel workers; the owner had asked earlier the same session "Tot el que puguis delegar, delegar-ho". The model change refers to the reviewer of the open native review `review-88ca0ea13149f65b` (v0.30.0 range), whose resilience lens had run out of output on `nan/deepseek-v4-flash`.

### L2 — Delegation (parent)

Three background workers with disjoint edit surfaces in the same tree, no commits by the workers: W-A `mwb4vrxp-1-8tz5` (T6, `scan/runner.py`), W-B `mwb4vs0m-2-0zv7` (T5, sources panel), W-C `mwb4vs2w-3-fks6` (T2-T4). The parent commits each unit after its report.

### L3 — T6 is unsafe as proposed (worker W-A)

The worker took the authorized "report rather than ship" path and changed nothing. A savepoint on the live connection is unsafe because the steps end transactions: the sync commits explicitly (`plex/sync.py:100-105`, `:152`, libraries written before later Plex requests) and the enrichment uses `with conn`, which commits on success (`enrichment/linking.py:122`, `enrichment/files.py:104`, `enrichment/history.py:29`), so a commit-method wrapper is not enough. Neutralizing every commit would hold the live writer lock from the first library upsert through the Plex network calls, enrichment, detection, re-check and the deletion preview, so the web actions would wait or fail; today's `conn.backup` takes no live writer lock for the pipeline work. The report row is also written on a separate live connection before the work connection closes (`scan/runner.py:147-155`, `:175-185`), so a transaction preview would block itself. Detection and snapshots only read; the re-check and deletion already bypass persistence in a dry run (`lifecycle/engine.py:390`, `:437`; `lifecycle/deletion.py:99`, `:121-144`). Existing scan suites: 13 passed. Decision (parent): #15 is not fixed in v0.31.0 and stays open; the issue itself says "Consider", and the measured cost (1-2 s per dry run) is below the risk of writing to the live library database.

### L4 — T2-T4 (worker W-C)

CSV: a value that starts with a raw tab or carriage return is escaped; `=`, `+`, `-`, `@` keep the check after leading whitespace. Users: `_users` is reset at the shared entry of a detection pass, so a reused engine sees user changes on the next pass and a pass still reads the users once. S6: six cases (a post-mark episode play cancels either marked season, one cancellation event, the season collection membership is cleared, timing boundaries, unrelated seasons, excluded users) pass without a production change. Suites: tests/web, tests/detection and tests/lifecycle 994 passed (shared tree, during parallel work). Commits `a620220`, `bd2cdf6`, `607e6ef`.

### L5 — T5 (worker W-B)

The enrichment file refresh records the disk sets (global, and per library with the panel's series-key join, orphan episodes counted globally) in the existing `app_meta` table, inside the same transaction as the file facts; the sync step invalidates them first, so a failed sync or refresh leaves the panel on its live query; a missing or malformed value also falls back to the live query, and an empty set is a valid answer. No schema change and no per-process cache, so every gunicorn worker reads the same value. Traces on a separate connection show no UNION and no `media_items`/`episodes` query after the value is recorded. Commit `9e9e424`.

### L6 — Release files, suite and the failed verifier (parent)

The release files were prepared by worker `mwb5h0p2-5-3pha` (version 0.31.0 in `pyproject.toml` and `almali_library_curator/__init__.py`, CHANGELOG entry, README status line, ROADMAP row; the parent reworded the Security line). Full suite on the integrated tree (job-1): 1493 passed, 10 skipped, exit 0; after the version bump, the About, package-data, CSV and sources-panel tests: 53 passed. The independent verifier `mwb5h0kz-4-mx7n` failed with "assistant reported an error" and no report, and was not retried. Owner choice (verbatim): "Revisor substitut i publicar (recomanat)" — a read-only reviewer of another role takes its place, as in v0.29.0, and the release is published if it passes.

### L7 — Session closed before publication (owner, verbatim: "guarda i tanco")

State at close: release commit `f17d1a0` (`chore: release v0.31.0`) is local only — no tag `v0.31.0`, nothing pushed, #12 still open, #15 not commented. The substitute reviewer `mwb6aca9-6-vqfy` (`jd-judge-a`) finished with an empty result (`{"rows":[]}`), so there is still no independent verdict and the release was not published. The runtime reported the range as an unreviewed candidate, so a native review was started over `cc50cde..f17d1a0` (committed only): lineage `review-bbb9e92b7c8e45ba`, tier medium, lenses risk/resilience/reliability, 18 paths, 784 lines, correction budget 200, state `reviewing`; its reviewer group has not been collected yet. This Log entry is left uncommitted so the committed candidate of that review does not move.

Next session: (1) bound STATUS of `review-bbb9e92b7c8e45ba`, collect the reviewer group directly (never from codemode), acknowledge if approved (the facade acknowledgement may answer `schema-incompatible`; then run the exact provider-issued CLI command); an approved native review can serve as the independent outside view the owner asked for ("Revisor substitut i publicar"); (2) if it passes, commit this Log, tag `v0.31.0` on the release commit, push the branch and the tag, wait for the publish run, close #12 with "Fixed and released in v0.31.0.", comment on #15 with the L3 evidence.

### L8 — Verification, the unavailable outside views, and the decision to publish (owner, verbatim: "Fes-ho")

The range is verified inline. No outside view could be collected in this runtime, and the provider's own plan says none is required for this candidate.

Native assess over the exact committed range `cc50cde..f17d1a0` (18 paths, 784 changed lines): risk medium, nativeRisk medium, `reviewDue: false` (`under_budget`), plan `writerSelfVerification: true`, `independentVerifier: false`; the reason states that no Task Size high-risk item is escalated, so the writer's self-verification stands and no separate verifier runs, whatever the native tier or the model that wrote the change. The ODD verifier gate agrees for this change: "This change declares none: run its focused test and suite inline yourself now".

The native ordinary review opened in the previous session (lineage `review-bbb9e92b7c8e45ba`, state `reviewing`) cannot be collected in this runtime: `gentle_review_capture_group` refuses the provider's own bindings ("collectBindings must be the complete distinct current reviewer group in exact provider order") and `gentle_review_capture` refuses the single slot ("collectBinding is missing or stale for current STATUS"), both with `mutation_performed: false`, while `gentle-ai review capture-result --preflight` accepts the same provider tokens and prints the frozen 18-path manifest. The defect is in the Pi capture facade, not in the candidate or in the provider; it is the second occurrence, the v0.30.0 lineage behaving identically. The substitute reviewers are unavailable as well: `jd-judge-a` returned an empty result, `jd-judge-b` timed out with zero tool calls, and the Claude Code session is expired (OAuth). No unchanged retry was made; the lineage stays open for a later session, and the contract was not bypassed.

The parent's inline review of the production code found no defect. CSV escaping (`web/routes.py:122`) escapes a raw tab or carriage-return prefix and still detects `= + - @` after leading whitespace, with no remaining injectable prefix. The users cache reset sits in `_detect` (`detection/engine.py:253`), the single funnel of `scan`, `scan_with_protection` and `scan_items`, so one pass reads the users once and a reused engine sees user changes on the next pass. The sources cache (`enrichment/sources.py`, `enrichment/files.py:132`, `scan/pipeline.py:72`, `web/views.py:281`) is computed from the same stored facts the panel's live query reads, inside the refresh's own transaction, while the only other writer of those facts (the Plex sync, `plex/sync.py:100`, `:119`, `:248`) is preceded by the invalidation in the sync step, so the panel can never serve a stale scope; a missing or malformed value falls back to the live query, an empty set is a valid answer, and the value lives in `app_meta`, so every worker reads the same one.

Evidence: focused suite on the integrated tree (job-1), 74 passed, exit 0 (`tests/web/test_csv_escaping.py`, `tests/web/test_sources_panel.py`, `tests/enrichment/test_sources_disks.py`, `tests/detection/test_users_cache.py`, `tests/lifecycle/test_recheck_episode_play.py`); full suite at L6, 1493 passed and 10 skipped; 53 passed after the version bump. Owner instruction verbatim: "Fes-ho". The next step is the publication of the release commit `f17d1a0` as `v0.31.0`.

### L9 — Publication of v0.31.0 (parent)

Annotated tag `v0.31.0` on the release commit `f17d1a0` ("v0.31.0: sources panel disk cache, CSV tab and carriage-return escaping, per-pass users cache (issue #12)"). The branch `feature/almali-library-curator-v1` was pushed (`cc50cde..9a771aa`) and the tag with it, one publish run at a time, so `latest` ends on v0.31.0. Publish run `43059234759` (tag `v0.31.0`, head `f17d1a0`) finished successfully in 2 m 46 s: the multi-arch image (linux/amd64, linux/arm64) was built, pushed to `ghcr.io/almali-dev/almali-library-curator` and signed with cosign. The registry digest cannot be confirmed from this machine: the token has no `read:packages` and the container API answers 404, as in the previous releases. The run annotations are informational (Node.js 20 deprecation on the runners, `ubuntu-latest` migration notice).

Issue #12 is closed with "Fixed and released in v0.31.0.". Issue #15 stays open with the L3 evidence: the dry-run preview still copies the database, because a savepoint/rollback preview is unsafe while the pipeline's steps commit explicitly and would hold the live writer lock through the whole scan, and because the report row is written on a separate live connection.
