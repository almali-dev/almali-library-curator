# v0.30.0 — web and scan performance (#7, #8, #9)

Authorized scope: issues #7, #8, #9 released as v0.30.0 on `feature/almali-library-curator-v1`; source feature: `odd/tasks/almali-library-curator.md`; previous ladder: `odd/tasks/issues-v029.md`.
Runner: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q`. Push, tag publication and the publish run follow the ladder strategy: push the branch and the tag, one publish run at a time, so `latest` ends on the newest release.

## Specs

### S1 — Owner authorization

> revisa on estem

> continua en automàtic

### S2 — #7: perf: the Library page computes every view twice and re-parses the report JSON

Source: https://github.com/almali-dev/almali-library-curator/issues/7. Exact issue text:

> `library_view` (`web/views.py:289-296`) calls `summary_view`, which already builds candidates, protected and leaving (`:214-216`), then builds all three again; each `_items` (`:35-59`) parses the whole report JSON and re-sorts it. Measured with 2,541 candidates (1.3 MB report): **243 ms and 372 SQL statements per render**, about half of it duplicated.
>
> **Suggested fix:** compute each sub-view once per request and cache the parsed report; later, store candidate rows in a table instead of one JSON blob.

### S3 — #8: perf: N+1 queries in detection and protection, and no index on protections(media_item_id)

Source: https://github.com/almali-dev/almali-library-curator/issues/8. Exact issue text:

> Measured at 3,501 candidates: 3,500 x `SELECT title, excluded FROM users` (`_user_count()`, `detection/engine.py:626-639`), 3,501 x `SELECT library_id FROM media_items WHERE id=?` (`protection/engine.py:206-210`), 2,541 x `SELECT detail FROM protections WHERE kind=? AND media_item_id=?` (`protection/engine.py:232-236`), which is a full `SCAN protections` because only the primary key is indexed. 9,549 statements per scan.
>
> **Suggested fix:** cache the user count, carry `library_id` on the item row, load keep-forever protections once into a dict, and add `CREATE INDEX ... ON protections(media_item_id)` in a migration.

### S4 — #9: perf: the History page loads every event and sorts without its index

Source: https://github.com/almali-dev/almali-library-curator/issues/9. Exact issue text:

> `web/views.py:221-231` selects all `removal_events` with `ORDER BY julianday(occurred_at) DESC, id DESC` and no `LIMIT`; the query plan is `SCAN removal_events | USE TEMP B-TREE FOR ORDER BY` because the index on `occurred_at` cannot be used through `julianday()`. 68 ms at 5,000 events and growing; the CSV export does the same (`routes.py:120-131`).
>
> **Suggested fix:** paginate the page and sort by the bare `occurred_at DESC, id DESC` so the index applies; stream the CSV.

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | The Library page builds every sub-view once per request and parses the scan report once | S2 | parent | done | `ec3fd66`; RED (the trace of one render counted 4 `scan_runs` and 4 `removal_requests` queries) → GREEN (1 and 2); the web suite 746 passed; a new test pins that the render reads the report and the leaving view once, and another pins the unchanged content of the page |
| T2 | Detection and protection stop the per-candidate queries and the protections index exists | S3 | parent | done | `aa7cd5d`; RED (6 user queries, 5 protection queries at 5 candidates) → GREEN (1 and 1); migration 7 adds `idx_protections_item`; the detection, protection, lifecycle and scan suites pass (84 + 163); Risk item 1: the protection answer must not change (independent verification in T4) |
| T3 | The History page paginates, sorts on the index and streams the CSV | S4 | parent (the worker failed) | done | `1c47578`; RED (no page parameter and no navigation) → GREEN; the page and the export read `SCAN removal_events USING INDEX idx_removal_events_instant`, with no temp B-tree; the new tests pin the pages, the filters, the order of the instants and the whole CSV export; the page number is clamped after the independent review (an `OverflowError` and a 500) |
| T4 | Independent verification of the risk items, version 0.30.0, CHANGELOG, roadmap, tag, publish | S1-S4 | parent + independent reviewer | done | Independent read-only review PASS for S2, S3 and S4 with file:line evidence; the differential against the v0.29.0 tree is identical; native assess over `9184aac..c2abee2`: risk high (escalation item 1), nativeRisk medium, `reviewDue: false` (`under_budget`), so no native review was required; a native review of the range was started later and stays open (`review-88ca0ea13149f65b`, see L10); the suite of the release 1419 passed/10 skipped (exit 0); release commit `c2abee2`, tag `v0.30.0`, publish run `42881489897` |

## Log

### L1 — Original owner request

> revisa on estem

> continua en automàtic

### L2 — Batch selection and current positions (parent)

Batch: #7, #8 and #9 are the remaining medium-priority performance issues of the Bolt pass (labels `performance`, `priority: medium`); #12 and #15 are low priority and their own text says they are fine for v1, so they stay open for a later release. The ladder rule of the roadmap gives the next free minor number, v0.30.0. The line references in the issue texts point at the pre-v0.29.0 file: the current positions are `library_view` (`web/views.py:362`), `summary_view` (`:151`), `_items` (`:52`), `history_view` (`:164`), `history_csv` (`web/routes.py:129`), `_user_count` (`detection/engine.py:789`), `_library_protection` (`protection/engine.py:206`) and `_keep_forever_protection` (`:232`).

### L3 — T1 implementation (parent)

The Library page built the candidates, the protected items and the leaving requests twice: `summary_view` built all three and `library_view` built them again, and `_items` parsed the whole scan report for each view. A page object (`_Page`) now holds the parsed report, the curated item ids and the built items, and `library_view` builds the three views once; the category filter is applied to the items it already built, so the summary still counts every candidate while the tab still shows one category. The new tests count the queries of one render with the SQLite trace callback: RED (4 `scan_runs` and 4 `removal_requests` queries) before the change, GREEN (1 and 2) after it. A second test pins that the page still shows the candidates, the protected items, the leaving requests and the summary totals. The web suite passes (746). Committed as `ec3fd66`.

### L4 — T2 implementation (parent)

The scan asked the users table once per candidate (`_user_count`: 3,500 statements at 3,501 candidates) and the protection pass asked for the library and the "Keep forever" scopes of every candidate (3,501 and 2,541 statements). The users are now read once per scan and counted from that list, the protection pass reads the library of every item and the "Keep forever" scopes once and hands them to the per-candidate decision, and migration 7 adds `idx_protections_item` for the single-item lookups. The single-candidate entry point (`protection_for`, the final re-check) keeps its own queries, which the index now serves. The iteration order of the "Keep forever" scopes stays the row order of the table, as before. RED (6 user queries and 5 protection queries at five candidates) → GREEN (1 and 1); the detection, protection, lifecycle and scan suites pass (84 + 163). Risk: item 1 — the protection pass gates the irreversible deletion, so its answer must not change; the independent verification is part of T4. Committed as `aa7cd5d`.

### L5 — T3 delegation (parent)

The History page reads every event and sorts it with `julianday()`, so the index on `occurred_at` cannot serve the order and the query plan shows a temp B-tree. The issue suggests the bare `occurred_at` order, but that changes the order of events written with different UTC offsets, which an existing test pins; so the parent keeps the instant-aware order and adds an expression index (`julianday(occurred_at), id`) that SQLite uses for it, with the pagination and the streamed CSV on top. Verified with EXPLAIN QUERY PLAN: `SCAN removal_events USING INDEX idx_removal_events_instant`, no temp B-tree. The task was delegated to a worker (`mwayixqw-2-w2n8`) with a bounded edit surface, because the parent's context had reached its backstop.

### L6 — T3 implementation (parent, after the worker failed)

The worker `mwayixqw-2-w2n8` failed without a report and left no change in the tree, so the parent implemented the task inline. The page reads 50 events at a time with `LIMIT`/`OFFSET` over the order of the instant, the count of the filtered events tells whether a next page exists, the model carries the page state and the template renders the navigation links, which keep the filters; the CSV export streams its rows instead of building the body in memory. Migration 8 adds the expression index `idx_removal_events_instant` on `removal_events(julianday(occurred_at), id)`, which SQLite uses for the order of both the page and the export (`EXPLAIN QUERY PLAN`: `SCAN removal_events USING INDEX idx_removal_events_instant`, no temp B-tree). RED (no page parameter and no links) → GREEN, six tests of its own. The independent review found that a page number above the range of SQLite raises an `OverflowError` and a 500; the page is now clamped to the last one, with a test of its own.

### L7 — Differential and independent review (parent)

Differential: the code before the release (`9184aac`, the v0.29.0 tree) and the code of this release answer the same on the same database (a protected library, a rated item, a "Keep forever" scope, a damaged scope and an unprotected item, with the users of the never-watched sentence and the Library page model): the JSON answers are identical, including the fail-closed protection of the damaged scope. Independent read-only review: PASS for S2, S3 and S4 with file:line evidence; it asked for the T3 row and this Log to be up to date before the tag. Non-blocking findings, recorded as follow-up: the users cache lives as long as the engine and not as long as the scan (the sync writes the users before the detection, so no answer changes); a single-item scan now reads every media item and every "Keep forever" scope, once per call; the escaping of the CSV checks `lstrip()` before the prefixes, so the tab and carriage return prefixes never match — that check predates this release (`9184aac`) and stays outside it.

### L8 — The protection pass reads only its own items (parent)

The independent review found that the protection pass read every media item and every "Keep forever" scope to answer for its candidates, so a pass over one item read the whole library — the single-item path of the re-check, which the v0.29.0 release had made fast. The pass now asks for its own items in chunks of 500 parameters, and the index of the migration 7 serves that lookup. The answers do not change: the differential against the v0.29.0 tree is still identical, and the protection and lifecycle suites pass (185).

### L9 — Delivery of v0.30.0 (owner, verbatim: "continua en automàtic")

Branch `feature/almali-library-curator-v1` pushed, annotated tag `v0.30.0` pushed, publish run `42881489897` completed successfully (multi-arch image built, pushed and signed), so `latest` ends on v0.30.0. Issues #7, #8 and #9 closed with "Fixed and released in v0.30.0." Slice boundary: the release is one slice of 687 changed lines over 16 paths (native assess) on top of the published v0.29.0 range, in six work-unit commits: `ec3fd66` (the Library page), `aa7cd5d` (the users and the protections), `1c47578` (the History page and the CSV), `85832a0` (the clamp of the page number), `eb4475c` (the pass reads only its own items) and `c2abee2` (the release). Open issues left: #12 and #15 (low priority, "fine for v1"). Follow-ups recorded: the users cache lives as long as the engine and not as long as the scan; the escaping of the CSV checks `lstrip()` before the prefixes, so the tab and carriage return prefixes never match — it predates this release and stays outside it.

### L10 — The native review of the v0.30.0 range (parent)

The runtime reported an unreviewed candidate in this worktree, so the parent inspected the authority and started an ordinary native review of the published range `9184aac..HEAD` (lenses risk, resilience and reliability; tier medium; 762 changed lines; correction budget 200). The lineage `review-88ca0ea13149f65b` is in the state "reviewing" with the three materialize slots of the group pending. The capture tools refuse every binding that the provider itself issues — the group and the single slot, with the exact strings and with the exact objects of a fresh STATUS — so the review cannot advance in this runtime: "collectBindings must be the complete distinct current reviewer group in exact provider order" and "collectBinding is missing or stale for current STATUS". No capture mutated anything. The release does not depend on that review: the native assess of the same range had said `reviewDue: false` (`under_budget`), so it was never required by the tier; the writer self-verification of the plan is the green suite of the release (1419 passed, 10 skipped) and its independent verifier is the read-only review (PASS for S2, S3 and S4) with the differential against the v0.29.0 tree. The open lineage is left for a later session; abandoning it is the owner's decision.

### L11 — The native review of the v0.30.0 range closes approved (parent)

Owner, verbatim: "Endavant. He canviat l'origen del model i l'esforç" (the reviewer model and effort were changed after the resilience lens had run out of output on `nan/deepseek-v4-flash`: `stopReason: length`, 16,384 output tokens, all reasoning). The capture group of `review-88ca0ea13149f65b` now accepts the exact STATUS bindings when it is called directly; the three reviewers ran (risk, resilience, reliability) and the review closed **approved** on the last admitted event. The facade acknowledgement answered `native-status-unavailable` (`schema-incompatible`), so the parent ran the exact provider-issued `gentle-ai review acknowledge-approved` command with the package CLI: `action: acknowledged`, `authority: burned`, consumed revision `sha256:f2f43cd1…`. Advisory, non-blocking findings (later work, not a reason to re-review): `R3-001` (`detection/engine.py:307-308`, the users cache; covered by v0.31.0 S5), `R4-history-page-snapshot-race` (`web/views.py:232-241`: the count and the page are two reads), `R4-stream-error-status` (`web/routes.py:140-145`: an error in the middle of the streamed CSV cannot change the 200 already sent), `R4-stream-read-transaction` (`web/routes.py:140-143`: the streamed export is not one read snapshot).
