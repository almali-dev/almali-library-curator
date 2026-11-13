# Scan storage and responsiveness

Scans keep full diagnostic reports for the **newest 30 attempts**, including
failed and skipped attempts (roughly one month with daily scans). Older full
reports are cleared automatically; lightweight run labels remain available to
the scheduler. Removal History is not pruned by this policy.

## Library display

The shell reads only run labels. Library reads a materialized candidate/protected
snapshot and small step-status projection, not the full diagnostic report. The
newest successful display remains available even after 30 failed attempts. Dry-run
snapshots still describe the preview, without changing the live media rows.

Schema v10 builds these projections from existing reports in an atomic migration,
protected by the usual pre-migration backup. Malformed old JSON produces an empty
display rather than preventing the upgrade.

## Reclaiming space

After pruning begins, every 30th recorded attempt runs SQLite `VACUUM` after the
recording transaction commits. This rebuilds an existing database safely, including
installations without auto-vacuum. Allow spare disk space for the rebuild (SQLite
can need up to twice the database size). A busy database or an unavailable rebuild
logs `scan_report_vacuum_deferred`; the report remains committed and maintenance
is retried at the next 30-attempt boundary. Database pages are reclaimed; migration
backups have their own five-copy retention policy.

## Concurrent owner actions

Plex sections, accounts and history finish fetching before a single database
write transaction publishes the snapshot. Mark, Keep and the kill switch can
therefore write while remote HTTP paging is in progress. Connections wait up to
30 seconds for other writers. Publishing a very large snapshot or periodic
`VACUUM` still requires the writer slot; this change does not promise lock-free
writes during those local operations. Fetch-first also holds the remote snapshot
in memory until publication.
