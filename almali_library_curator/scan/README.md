# Scan storage and responsiveness

Scans keep full diagnostic reports for the **newest 30 attempts**, including
failed and skipped attempts (roughly one month with daily scans). Older full
reports are cleared automatically; lightweight run labels remain available to
the scheduler. Removal History is not pruned by this policy.

## Library display

The shell reads only run labels. Library reads a materialized display snapshot
(every section of the run's detection counts) and a small step-status
projection, not the full diagnostic report. The newest successful display
remains available even after 30 failed attempts. Dry-run snapshots still
describe the preview, without changing the live media rows.

The materialized projection keeps **every** section of the run's detection counts,
not only the candidate and protected rows, so no section the page used to read is
silently lost. A section the run does not carry (or a damaged report) reads as an
empty list, never as a missing value. Schema v10 builds these projections from
existing reports in an atomic migration, protected by the usual pre-migration
backup. Malformed old JSON produces an empty display rather than preventing the
upgrade. Schema v11 completes the projection with the remaining count sections;
a run whose full report was already pruned keeps its materialized display.

The labels of the newest finished run carry a `report` key: the run's step
statuses as JSON text (`{"sync": "finished", ...}`), at most five short entries.
It never carries the full diagnostic report, which stays pruned.

## Reclaiming space

After pruning begins, every 30th recorded attempt runs SQLite `VACUUM` after the
recording transaction commits. This rebuilds an existing database safely, including
installations without auto-vacuum. Allow spare disk space for the rebuild (SQLite
can need up to twice the database size). The rebuild is maintenance, not an owner
write: it lowers its own busy timeout to one second and restores the connection's
30 seconds afterwards. A writer that already holds the slot therefore defers the
rebuild instead of queueing the scan behind it for 30 seconds. A busy database or
an unavailable rebuild logs `scan_report_vacuum_deferred`; the attempt stays
committed, the scan never fails for it, and maintenance is retried at the next
30-attempt boundary. Database pages are reclaimed; migration backups have their
own five-copy retention policy.

## Concurrent owner actions

Plex sections, accounts and history finish fetching before the sync writes any
row. The whole snapshot is then published in one transaction, so the sync holds
SQLite's writer slot only for the local writes. Mark, Keep and the kill switch
can therefore write while remote HTTP paging is in progress. Connections wait up
to 30 seconds for other writers. Publishing a very large snapshot still requires
the writer slot; this change does not promise lock-free writes during that local
operation. Periodic `VACUUM` keeps the slot only for the rebuild it can start
within one second: a held writer defers it (`scan_report_vacuum_deferred`) rather
than stalling owner writes behind a 30-second wait, but a running rebuild still
excludes them for its own duration. Fetch-first also holds the remote snapshot
in memory until publication.

The write transaction is the sync's own when the connection it is given has no
open transaction. A connection that already has one keeps it: the sync joins
that transaction with a savepoint, and it never commits or rolls back the
caller's pending work. The caller owns that transaction and commits it.
