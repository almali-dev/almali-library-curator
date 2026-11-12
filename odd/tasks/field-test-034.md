# 0.34 Field test — the first real-server run and the contract fixes it found

Milestone 0.34 of the pre-1.0 audit of v0.32.0 (`odd/audits/pre-1.0-audit-v0.32.0.md`).
The field test started on the owner's Unraid server on 2026-11-12 with v0.32.0 deployed in dry
run. Delivery: the contract fixes the first real run found, each test-first, then a release so
the field test can continue.

## Specs

### S1 — #37 Plex file paths and sizes are read from a top-level `Part`, not `Media[].Part[]`

Verbatim from the audit:

> Plex file paths and sizes are read from a top-level `Part`, not `Media[].Part[]`

> Unverified against a live server

Now verified, on 2026-11-13, against the owner's live server: the payload of
`GET /library/sections/<key>/all?type=1` carries **no** top-level `Part`. The file lives in
`Media[].Part[]`, and that entry carries `container, duration, file, id, indexes, key, size,
videoProfile`. The first scan of the deployed v0.32.0 failed in the sync step with a
`ValidationError`, so no library, no item and no candidate was ever produced, and the owner's
19 real sections were invisible to the app.

The fake Plex server used by the tests serves the top-level shape, which is why the whole suite
passed while the real server failed.

### S2 — the first-run flow must be a real step by step

Owner, verbatim:

> Res intuitiu el procediment. Hauria de ser un pas a pas real, no una scape room

The owner pressed the scan button, and nothing said that the scan had failed: the wizard then
dead-ended at Libraries with "Todavía no hay bibliotecas. Conecta Plex y ejecuta un análisis;
después vuelve a este paso". The audit describes the same gap in #45 (a running or failed scan
is invisible) and #46 (the first-run wizard dead-ends at Libraries).

### S3 — the Plex fallback must not delete a multi-version item

The first real-server verification (2026-11-13) found that **199 of the owner's 9,205 items carry
more than one `Media` entry** (39 movies and 160 episodes; from 2 up to 43 versions), every
version with its own distinct file, and that the API carries **no marker** of which version is the
primary one. The app stores one path and one size per item, so the marked scope covers one file
while `DELETE /library/metadata/<ratingKey>` removes the whole item, every version included. Before
the contract fix of S1 those items failed safe (no path, so an empty scope); with it they can be
deleted.

Requirement: before any Plex metadata deletion the item is read again, and the request refuses
when the item carries more than one version with distinct files, because the marked scope cannot
cover them. The refusal uses a fixed code and sends nothing. A single-version item keeps the
current behaviour. The "the first `Media` entry is the primary version" rule is an assumption the
API does not prove, so no deletion may depend on it.

## Tasks

| ID | Outcome | Specs | Route | Status | Commit / evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | The Plex metadata parser reads the file and its size from `Media[].Part[]`, and the fake server serves the real shape | S1 | worker | pending | Risk: item 3 (the contract the rest of the app consumes) |
| T2 | The scan outcome and its failure are visible, and the wizard guides the owner step by step | S2 | worker | pending | Risk: item 6 (no test would catch a regression in what changes) |
| T3 | The Plex fallback refuses a multi-version item before any metadata deletion | S3 | worker | done | `f0462d4`; the refusal code `plex_multi_version` sends nothing, the count does not depend on the document order, and its three catalog strings are merged once the UX unit releases the catalogs |

## Log

### L1 — The first real run on the owner's server

Owner, verbatim:

> Res intuitiu el procediment. Hauria de ser un pas a pas real, no una scape room

Evidence gathered on 2026-11-13, read-only: the deployed v0.32.0 answered `/healthz` with
`{"missing":[],"ready":true}`, the owner's Plex server answered the live probe with 19 sections
(movie, series and mixed), and the first scan row in the database records the sync step as
`failed` with `ValidationError` and zero libraries, zero items and zero candidates. The live
payload has `Media[].Part[]` and no top-level `Part`.

### L2 — The verification against the live server found a deletion gap the tests could not

The independent verification of the contract fix (`mwe6562x-f-98f5`, read-only against the owner's
server) measured the owner's library: **9,205 items, 199 of them with more than one `Media` entry**
(39 movies, 160 episodes; 175 with two versions, 21 with three, and some with 5, 14 and even 43),
every version with its own distinct file, and **no marker anywhere in the payload of which version
is the primary one**. The list order and the single-item order agree, so the rule "the first
`Media` entry is the primary version" is a stable document order, not a fact the API proves.

The app keeps one path and one size per item, while the Plex fallback deletes with
`DELETE /library/metadata/<ratingKey>`, which removes the whole item and every version. Before
the contract fix of S1 those items failed safe; with it, they could have been deleted outside the
verified scope. The parent's decision: a deletion may not depend on the document order, so the
item is read again before the deletion and the request refuses with a fixed code when more than
one version carries a distinct file.

The verification's own verdict on the parser itself was clean: it reads `Media[].Part[]`, keeps the
top-level `Part` as a documented fallback, no other consumer of the shape was left behind, and the
fake server now serves the real shape so the tests cannot pass against a contract the live server
does not have.
