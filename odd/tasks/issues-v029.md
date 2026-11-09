# v0.29.0 — detection performance (#4, #5, #6)

Authorized scope: issues #4, #5, #6 released as v0.29.0 on `feature/almali-library-curator-v1`; source feature: `odd/tasks/almali-library-curator.md`.
Runner: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q`. Push, tag publication and the publish run stay owner decisions.

## Specs

### S1 — Owner authorization

> Val, esborra tots els worktrees. I anem pels, 4,5 i 6 com a versió 29

### S2 — #4: perf: detection scales with items x play events (47 s at 120k plays)

Source: https://github.com/almali-dev/almali-library-curator/issues/4. Exact issue text:

> `detection/engine.py:183-202` filters the whole flat `events` list once per series and once per movie, and `_episodes_of` (`:230-234`) rescans the episode index per series. Measured `_detect()`: **47.1 s with 120,000 play events, 20.6 s with 60,000** (5,000 movies, 300 series). It grows linearly with history, so a long Tautulli history makes every scan slower.
>
> **Suggested fix:** group events by series key and by movie key once in `_load_events`, and episodes by series in `_load_episode_index`. Expected: scan compute under 2-3 s.

### S3 — #5: perf: the final re-check runs a full detection scan for every due request

Source: https://github.com/almali-dev/almali-library-curator/issues/5. Exact issue text:

> `lifecycle/rematch.py:35` calls `self.engine.scan()` inside `still_matches()`, which `run_recheck` calls once per due request (`lifecycle/engine.py:331-366`). One call measured **44.8 s** at 120k plays; thirty items due on the same day would spend about 22 minutes inside one scan run.
>
> **Suggested fix:** run one scan per re-check pass, index its candidates by `(media_item_id, unit, seasons)`, and let the rematcher look them up. Expected: N scans become 1.

### S4 — #6: perf: every "Mark for removal" click runs a full detection and protection scan

Source: https://github.com/almali-dev/almali-library-curator/issues/6. Exact issue text:

> The mark action builds a fresh candidate with `detection.scan_with_protection()` (`web/actions.py:64-71`, used at `:414`). Each click blocks the request for **20-47 s** on a library of 5,000 movies.
>
> **Suggested fix:** validate the submitted scope against the stored scan report (`scan_runs.report`) or run detection for that single item. Expected: the click answers in milliseconds.

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | Group events and episodes once per scan; add a single-item scan equal to the full scan restricted to those items | S2 | parent | done (uncommitted→see L3) | RED 9 failed → GREEN; full suite 1406 passed/10 skipped; differential old vs new on 5,000 movies/300 series/120k plays: full scan 31.02 s → 1.09 s, output identical (260 rows), single-item scan equal to the full scan on 60 sampled items, worst 4 ms |
| T2 | The final re-check rematches each due request with a single-item scan | S3 | parent | done (uncommitted→see L3) | RED 9 failed → GREEN; full suite 1406 passed/10 skipped; differential old vs new on 5,000 movies/300 series/120k plays: full scan 31.02 s → 1.09 s, output identical (260 rows), single-item scan equal to the full scan on 60 sampled items, worst 4 ms |
| T3 | Mark for removal re-derives its candidate with a single-item scan | S4 | parent | done (uncommitted→see L3) | RED 9 failed → GREEN; full suite 1406 passed/10 skipped; differential old vs new on 5,000 movies/300 series/120k plays: full scan 31.02 s → 1.09 s, output identical (260 rows), single-item scan equal to the full scan on 60 sampled items, worst 4 ms |
| T4 | Independent verification (Risk item 1: the re-check gates irreversible deletion), version 0.29.0, CHANGELOG, roadmap, local tag | S1-S4 | parent + verifier | pending | |

## Log

### L1 — Original owner request

> Val, esborra tots els worktrees. I anem pels, 4,5 i 6 com a versió 29

### L2 — Design decisions (parent)

(a) #5 deviates from the issue's suggestion: instead of one cached scan per re-check pass, each due request is rematched with a scan restricted to its own media item. The cost is milliseconds per request, and no pass-level cache can go stale between a cancellation and the next request just before an irreversible deletion. (b) #6 takes the issue's second option (single-item detection), not the stored report, so the mark still validates against the current database. (c) The single-item scan applies the same library scope, users, thresholds, enrichers and protector as the full scan; its candidates must equal the full scan's candidates for those items, which is what the tests pin.

### L3 — Session close (owner, verbatim: "guarda i tanco. He de marxar a dormir")

T1–T3 implemented inline, test-first: `DetectionEngine.scan_items(ids)` restricts the full scan (items, episodes, movie keys and play events read through `json_each` on the rating-key index) and `_detect` groups episodes and events once per scan (#4); `DetectionRematcher.still_matches` calls `scan_items([media_item_id])` (#5); `web/actions._fresh_candidate` calls `scan_items([scope.media_item_id])` (#6). New tests: `tests/detection/test_single_item_scan.py` (equality with the full scan per item and for several items, curated scope, empty/unknown ids, characterization of the full scan captured from the old code), two re-check tests and a rematcher scope test in `tests/lifecycle/test_no_match.py`, one mark test in `tests/web/test_actions.py`. Evidence: RED 9 failed before the change; full suite 1406 passed, 10 skipped; differential check (scratch copy of HEAD `33283aa` vs the change, same synthetic database of 5,000 movies, 300 series, 120,000 play events): full scan 31.02 s → 1.09 s with identical output (260 rows), single-item scan identical to the full scan on 60 sampled items, worst 4 ms. Committed as one work unit (Risk: item 1, the re-check gates irreversible deletion). Pending (T4): independent `gentle-ai-verify` for item 1, native assess/review if due, then version 0.29.0, CHANGELOG, README, ROADMAP, release commit and local tag; push and publish stay owner decisions. Note: the differential scratch directory under /tmp does not survive a reboot.
