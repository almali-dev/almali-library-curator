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
| T4 | Independent verification (Risk item 1: the re-check gates irreversible deletion), version 0.29.0, CHANGELOG, roadmap, local tag | S1-S4 | parent + verifier | done | Independent read-only review PASS for S2, S3 and S4 with file:line evidence (the `gentle-ai-verify` subagent was unavailable: stalled with no tool calls, reported, not retried); focused 45 passed, full suite 1406 passed/10 skipped (exit 0); native assess over 33283aa..7fea30a: risk high (agent escalation item 1), nativeRisk medium, `reviewDue: false` (`under_budget`), so no native review was started; release commit `7fea30a`, tag `v0.29.0` |

## Log

### L1 — Original owner request

> Val, esborra tots els worktrees. I anem pels, 4,5 i 6 com a versió 29

### L2 — Design decisions (parent)

(a) #5 deviates from the issue's suggestion: instead of one cached scan per re-check pass, each due request is rematched with a scan restricted to its own media item. The cost is milliseconds per request, and no pass-level cache can go stale between a cancellation and the next request just before an irreversible deletion. (b) #6 takes the issue's second option (single-item detection), not the stored report, so the mark still validates against the current database. (c) The single-item scan applies the same library scope, users, thresholds, enrichers and protector as the full scan; its candidates must equal the full scan's candidates for those items, which is what the tests pin.

### L3 — Session close (owner, verbatim: "guarda i tanco. He de marxar a dormir")

T1–T3 implemented inline, test-first: `DetectionEngine.scan_items(ids)` restricts the full scan (items, episodes, movie keys and play events read through `json_each` on the rating-key index) and `_detect` groups episodes and events once per scan (#4); `DetectionRematcher.still_matches` calls `scan_items([media_item_id])` (#5); `web/actions._fresh_candidate` calls `scan_items([scope.media_item_id])` (#6). New tests: `tests/detection/test_single_item_scan.py` (equality with the full scan per item and for several items, curated scope, empty/unknown ids, characterization of the full scan captured from the old code), two re-check tests and a rematcher scope test in `tests/lifecycle/test_no_match.py`, one mark test in `tests/web/test_actions.py`. Evidence: RED 9 failed before the change; full suite 1406 passed, 10 skipped; differential check (scratch copy of HEAD `33283aa` vs the change, same synthetic database of 5,000 movies, 300 series, 120,000 play events): full scan 31.02 s → 1.09 s with identical output (260 rows), single-item scan identical to the full scan on 60 sampled items, worst 4 ms. Committed as one work unit (Risk: item 1, the re-check gates irreversible deletion). Pending (T4): independent `gentle-ai-verify` for item 1, native assess/review if due, then version 0.29.0, CHANGELOG, README, ROADMAP, release commit and local tag; push and publish stay owner decisions. Note: the differential scratch directory under /tmp does not survive a reboot.

### L4 — Status review and T4 execution (owner, verbatim: "revisa on estem", then "continua en automàtic")

Status review: branch `feature/almali-library-curator-v1` at `b6a04f1`, two commits ahead of origin, clean tree, v0.28.0 published, no worktrees and no running subagents left; T4 was the only open task, so it was executed. The `gentle-ai-verify` subagent (`mwawy1yv-1-t2rd`) stalled with zero tool calls and was reported unavailable instead of being retried, and an independent read-only reviewer took its place: PASS for S2, S3 and S4 with file:line evidence — the scope filter reaches every loader (`engine.py:255`, `:326`, `:364`, `:446`), the SQL play-event filter can only remove rows the Python filter would remove (`engine.py:385-389` against `:395-398`), both rating-key columns and the `json_each` result are TEXT (`db.py:63`, `:88`), the protection engine still runs per candidate (`protection/engine.py:96-97`), and empty or unknown ids yield no candidates (`engine.py:320`, `:361`, `:386-387`, `:440`). It left one non-blocking test gap, recorded as follow-up: the re-check tests cover a movie without protector, and no test pins "a new play on an episode cancels a seasons request". Inline evidence: focused 45 passed; full suite 1406 passed, 10 skipped, exit 0. Native assess over the committed range `33283aa..7fea30a`: risk high (agent escalation item 1), nativeRisk medium, `reviewDue: false` (`under_budget`), so no native review was started; the plan's independent verifier is the review above. Release preparation: version 0.29.0 in `pyproject.toml` and `almali_library_curator/__init__.py`, CHANGELOG entry, README status line and ROADMAP milestone row, committed as `7fea30a` (`chore: release v0.29.0`); annotated tag `v0.29.0` ("v0.29.0: detection performance (issues #4, #5, #6)"). Slice boundary: this release is one slice of 486 changed lines over 12 paths (native assess) on top of the published v0.28.0 range; the branch itself carries the whole pre-1.0 history and is delivered as one milestone per tag. Delivery strategy, carried from the previous ladder: push the branch and the tag, one publish run at a time, so `latest` ends on v0.29.0. The 120,000-play differential measurement stands from the implementation session (31.02 s to 1.09 s); its scratch copy did not survive the reboot and was not re-run, so the fresh evidence here is the grouped-scan structure confirmed by reading and the green suite.
