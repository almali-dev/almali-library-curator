# v0.32.0 — stale detection cache and a flaky test (#34, #35)

Authorized scope: issues #34 and #35 released as v0.32.0 on `feature/almali-library-curator-v1`; previous ladder: `odd/tasks/issues-v031.md`.
Runner: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q`. Push, tag publication and the publish run follow the ladder strategy: push the branch and the tag, one publish run at a time, so `latest` ends on the newest release.

## Specs

### S1 — Owner authorization

> Continua, despŕes amb mes issues

Context: v0.31.0 was published and two independent reviews passed it. The owner authorized continuing and asked for more issues. The GitHub issues of the Bolt pass are exhausted (only #15 was still open), so this batch starts from the findings of the v0.31.0 review and from the known flaky test: #34 and #35 are fixed, #15 and #36 stay open with their evidence.

### S2 — #34: the series status cache lives as long as the detection engine

Source: https://github.com/almali-dev/almali-library-curator/issues/34. Exact issue text:

> `almali_library_curator/detection/engine.py:207` keeps `_status_cache` for the whole life of the `DetectionEngine`, so a reused engine serves the series status it asked for during its first pass.
>
> The users cache had the same shape and was scoped to one detection pass in v0.31.0 (`detection/engine.py:253`). The series status cache was left behind.
>
> Why it matters: `_series_status` (`engine.py:845-855`) feeds the series-status detections ("never watched", "every season finished"), which decide which items become removal candidates and which removals the re-check cancels. A stale status can therefore point a candidate at the wrong decision.
>
> Current exposure is limited: the engine is built per scan (`scan/pipeline.py:51`) and per request (`web/actions.py:78`), so in practice one engine serves one pass. The defect is latent: any future reuse of an engine across passes would serve a stale status, exactly the bug the users cache had.
>
> Suggested fix: reset `_status_cache` at the same shared entry point of a detection pass that resets `_users` (`_detect`), keeping one enricher call per series within a pass.

### S3 — #35: the host filesystem disk-usage test compares two live readings of the same disk

Source: https://github.com/almali-dev/almali-library-curator/issues/35. Exact issue text:

> `tests/hostfs/test_filesystem.py:154-172` reads the disk usage of a real root twice and compares the two readings for equality:
>
> ```python
> usage = host_fs.disk_usage("disk2")
> raw = psutil.disk_usage(os.fspath(unraid_tree["unraid"] / "disk2"))
> assert usage.used == raw.used
> assert usage.free == raw.free
> assert usage.percent == raw.percent
> ```
>
> The adapter and `psutil` are two separate reads of the same live filesystem. Any write between them (the test fixtures, the pytest cache, another process on the machine) changes `used`, `free` and `percent`, so the assertion fails on a busy machine even though the adapter is correct. The test passes when the disk is quiet, which is why the flakiness is rare and hard to see.
>
> Suggested fix: bracket the adapter's reading between two raw readings and assert the adapter's numbers fall inside that bracket (or differ by at most a small epsilon), so the test proves the adapter reports what the filesystem says without depending on the disk staying still.
>
> Found while preparing the v0.32.0 batch; it was already known as a flaky test before v0.31.0.

### S4 — #36 stays open with its evidence (not fixed in v0.32.0)

Source: https://github.com/almali-dev/almali-library-curator/issues/36. Exact issue text:

> `almali_library_curator/enrichment/sources.py:49` invalidates the recorded disk sets by storing the literal `'null'` in `app_meta`, before the Plex sync runs (`scan/pipeline.py:71-79`). The file refresh then records the real value in the same transaction as the file facts (`enrichment/files.py:132`).
>
> While the value is `'null'`, `cached_disks` returns `None` and every render of the sources panel falls back to the full UNION over `media_items` and `episodes` (`web/views.py:290-301`) — the exact cost issue #12 removed.
>
> If the sync or the refresh keeps failing, the row stays `'null'` and the panel stays on that fallback until a refresh succeeds. The panel is never wrong (that is the point of the invalidation), but it becomes slow again, with no signal to the user that it is serving the slow path.
>
> Suggested fix: decide whether the panel should show that it is on the fallback (a small notice next to the disk count, or a recorded timestamp), and whether a successful sync without a file refresh should be enough to leave the panel on the live query instead of the recorded one. Any change must keep the rule that a failed sync or refresh never leaves a stale scope.

Decision (parent): not fixed in v0.32.0. The slow path is the correct answer when the facts cannot be trusted; the only safe changes are a notice to the user or a recorded timestamp, and both belong with a decision about what the panel shows. The issue stays open with this evidence.

### S5 — Release and delivery of v0.32.0

Version 0.32.0 in `pyproject.toml` and `almali_library_curator/__init__.py`, a CHANGELOG entry, the README status line and the ROADMAP row, the annotated tag `v0.32.0` on the release commit, the branch and the tag pushed, one publish run watched to success, and issues #34 and #35 closed with "Fixed and released in v0.32.0.". #15 and #36 stay open with their evidence.

### S6 — Blind review of the v0.31.0 range: two actionable findings for this batch

The blind judge B reviewed the published v0.31.0 range `cc50cde..f17d1a0` and returned VERDICT: PASS (a 350-case differential fuzz found no divergence between the cached and the live disk counts). Two of its findings are actionable and belong to this batch; the rest are informational.

S6.1 — The sync-invalidate then refresh-record composition is never exercised end to end. `tests/web/test_sources_panel.py:40-44` replaces the sync step with a stub, so the invalidation never runs there, and the only test that runs the real `steps['sync']()` (`tests/enrichment/test_sources_disks.py:76-101`) forces the refresh to fail, so it pins the fallback but never the re-record after a successful refresh that follows a real sync. The judge verified the path works with its own read-only probe, so this is a coverage gap, not a defect. The test must exercise a real sync followed by a successful scoped refresh, and prove the panel then answers from the recorded value (no UNION, no `media_items`/`episodes` query) with the same count the live query would give.

S6.2 — The episode-play test hardcodes a rendered date. `tests/lifecycle/test_recheck_episode_play.py` asserts the reason string with "2 November", derived from `tests/lifecycle/lifecycle_helpers.py:16` (`NOW = 2026-11-02T12:00Z`) plus the one-second offset. A future change of `NOW` to a late-evening instant would move the asserted day and break the test for the wrong reason. The expected day must be derived from the frozen clock instead of written as a literal.

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | Feature document, Engram mirror, todo | S1 | parent | done | this file |
| T2 | The series status cache is scoped to one detection pass | S2 | worker `mwcg0b5t-6-cu8c` | done | `7cf2be7`; RED 12 failed / 3 passed -> GREEN 15 passed; `tests/detection` 75 passed; the parent re-ran the focused file (15 passed) and read the diff |
| T3 | The disk-usage test does not depend on the disk staying still | S3 | worker `mwcg1bnk-7-lddu` | done | RED 2 failed (a real 4 MiB write and a moved raw reading both broke `usage.used == raw.used`) -> GREEN 26 passed; the adapter's reading is now bracketed between two raw readings, `total` stays an equality; the parent re-ran the file 3x and the class 5x with a concurrent 64 MiB write |
| T4 | #36 stays open with its evidence (not fixed) | S4 | parent | done | the issue body carries the analysis |
| T5 | Verification, version 0.32.0, CHANGELOG, README, roadmap, tag, publish, close #34 and #35 | S5 | parent + verifier | pending | |
| T6 | A real sync followed by a successful refresh re-records the disk sets, and the panel answers from them | S6.1 | worker | pending | |
| T7 | The episode-play test derives its expected day from the frozen clock | S6.2 | worker | pending | |
| T8 | The v0.31.0 task table stops claiming that #15 was closed | S6 | parent | pending | |

## Log

### L1 — Original owner request

> Continua, despŕes amb mes issues

### L2 — The batch (parent)

The GitHub issues of the Bolt pass are closed except #15, so the batch comes from the v0.31.0 review findings and the known flaky test. Three issues were filed on 2026-11-11: #34 (the series status cache lives as long as the engine), #35 (the disk-usage test compares two live readings) and #36 (the sources panel stays on the slow fallback while sync and refresh keep failing). #34 and #35 are fixed here; #36 and #15 stay open with their evidence, the same way #15 stayed open after v0.31.0.

Scope check before delegating: the only other cache of the same class is the TMDB movie-details cache in `enrichment/enrichers.py:43`, whose docstring already declares it scan-scoped ("One instance is scan-scoped: cached answers and failures avoid repeated network reads within detection"), and the enricher instance is built per scan (`scan/pipeline.py:48`) and per request (`web/actions.py:78`); it is not a defect and is not in this batch. The engine's `_status_cache` is different: the engine can outlive a pass, and the users cache proved that.

### L3 — The blind judge B passes the v0.31.0 range (parent)

The blind judge B (task `mwcfqdt2-5-tusv`) reviewed the published v0.31.0 range `cc50cde..f17d1a0` and returned VERDICT: PASS. It traced every changed production line, ran the five focused files (74 passed in 27.66 s), `tests/enrichment` + `tests/scan` (71 passed) and `tests/detection` + `tests/lifecycle` (210 passed), and stress-tested the central claim of S2 with a differential fuzz: 200 random database states times 7 scopes, plus 150 states where the recorded refresh scope differs from the queried scope (deselected libraries, `NULL` and empty `disk`, orphan episodes, missing library rows, `series` and `season` rows) found **0 mismatches** between the cached count and the live `_source_disks` count.

Its findings: F1 the sync-invalidate then refresh-record composition is never exercised end to end (a coverage gap; the judge probed the path with its own script and it works) — S6.1; F2 the v0.31.0 task table says T7 closes "#12 and #15" while #15 stays open, so the table alone misleads — T6; F3 the invalidated row stays `'null'` while sync and refresh keep failing, so the panel is slow but never wrong — already filed as issue #36; F4 a dry run writes the pre-existing `scan_runs` row and the lock file on the live database, unchanged by the range, and the new invalidation only touches the private preview; F5 a final re-check now issues one `users` query per marked request instead of one per engine, correct per-pass semantics on a small table; F6 the episode-play test hardcodes the rendered day — S6.2.

No blocker and no major finding. The release stands, and the two actionable findings join this batch.
