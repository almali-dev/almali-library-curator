"""SQLite storage: connection handling and forward-only migrations.

The schema version lives in SQLite's ``user_version`` pragma, so an existing
installation can be migrated in place without a separate bookkeeping table.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 15

# Full reports are bounded; lightweight run metadata remains available to the
# scheduler. The newest successful display survives even a long failure streak.
SCAN_REPORT_RETENTION = 30

# #110: WAL makes ``synchronous=NORMAL`` durable enough — a committed
# transaction survives a process crash, only a power loss can drop the last
# commits — while it spares a file system sync per commit. The page cache
# grows from SQLite's 2 MiB default to 16 MiB, the pages read the same few
# indexes over and over. Both pragmas are per connection, so ``connect`` sets
# them every time, and NORMAL only when the database really runs under WAL.
SYNCHRONOUS = "NORMAL"
CACHE_SIZE_KIB = 16_384

# Extract display projections once, on migration/write, never on a page read.
# The projection keeps the whole detection.counts object: the candidate and
# protected rows are only two of its sections, and a section the projection
# left out would silently disappear from the page.
_SCAN_VIEW_BODY = "CASE WHEN json_valid(report) THEN report ELSE '{}' END"
_SCAN_VIEW_SNAPSHOT = "json_object('counts', json_extract(body, '$.detection.counts'))"
_SCAN_VIEW_SELECT = f"""
    WITH valid AS (
        SELECT id, {_SCAN_VIEW_BODY} AS body
        FROM scan_runs
    )
    SELECT id,
        {_SCAN_VIEW_SNAPSHOT},
        json_object('sync', json_extract(body, '$.sync.status'),
                    'enrichment', json_extract(body, '$.enrichment.status'),
                    'detection', json_extract(body, '$.detection.status'),
                    'recheck', json_extract(body, '$.recheck.status'),
                    'deletion', json_extract(body, '$.deletion.status'))
    FROM valid
"""

# #48: the latest published snapshot as typed, indexed rows, so the Library
# pages, filters and counts in SQL instead of rendering every candidate.
# The rows are a derived cache, never a second source of truth: the backfill
# below builds them from the snapshot that is already published, and the
# triggers rebuild them whenever that snapshot is written (the scan's own
# publish, an import or a repair), inside the same transaction.
_SCAN_ROW_COLUMNS = (
    "media_item_id, unit, seasons, title, type_badge, categories, reasons,"
    " reason, reason_parts, size_bytes, disk, last_played_at, rating,"
    " rating_source, protection_kind, protection_reason, protection_reason_parts"
)

# The run the Library shows: the newest recorded finished run.
_LATEST_FINISHED_RUN = "(SELECT MAX(id) FROM scan_runs WHERE status = 'finished')"


def _scan_rows_insert(snapshot: str, run_id: str, kind: str) -> str:
    """The INSERT that turns one displayed section of a snapshot into rows.

    Every value is read defensively: a row the detection engine did not
    write in full still lands as a degraded row instead of failing the
    transaction that publishes the scan.
    """
    section = f"{kind}_rows"
    protection = (
        "json_extract(j.value, '$.protection_kind'),"
        " json_extract(j.value, '$.protection_reason'),"
        " json_extract(j.value, '$.protection_reason_parts')"
        if kind == "protected" else "NULL, NULL, NULL"
    )
    return f"""
    INSERT INTO scan_rows (run_id, kind, position, {_SCAN_ROW_COLUMNS})
    SELECT {run_id}, '{kind}', j.key,
        COALESCE(json_extract(j.value, '$.media_item_id'), 0),
        COALESCE(json_extract(j.value, '$.unit'), ''),
        COALESCE(json_extract(j.value, '$.seasons'), '[]'),
        COALESCE(json_extract(j.value, '$.title'), ''),
        COALESCE(json_extract(j.value, '$.type_badge'), ''),
        COALESCE(json_extract(j.value, '$.categories'), '[]'),
        COALESCE(json_extract(j.value, '$.reasons'), '[]'),
        COALESCE(json_extract(j.value, '$.reason'), ''),
        json_extract(j.value, '$.reason_parts'),
        COALESCE(json_extract(j.value, '$.size_bytes'), 0),
        json_extract(j.value, '$.disk'),
        json_extract(j.value, '$.last_played_at'),
        json_extract(j.value, '$.rating'),
        json_extract(j.value, '$.rating_source'),
        {protection}
    FROM json_each(COALESCE(json_extract({snapshot}, '$.counts.{section}'),
                            json_extract({snapshot}, '$.{section}'), '[]')) AS j
    WHERE j.type = 'object';
    """


def _scan_rows_publish(snapshot: str, run_id: str) -> str:
    """Rebuild the whole cache from one published snapshot.

    Only the latest published snapshot is kept, so a page never has to
    choose between runs, and the cache never grows with the retention.
    """
    return (
        "DELETE FROM scan_row_categories;\n"
        "DELETE FROM scan_rows;\n"
        + _scan_rows_insert(snapshot, run_id, "candidate")
        + _scan_rows_insert(snapshot, run_id, "protected")
        + "INSERT INTO scan_row_categories (scan_row_id, kind, category)\n"
          "    SELECT scan_rows.id, scan_rows.kind, category.value FROM scan_rows,"
          " json_each(scan_rows.categories) AS category\n"
          "    WHERE category.type = 'text';"
    )


# how many pre-migration database copies to keep in <config dir>/backups
BACKUP_RETENTION = 5

MIGRATIONS: tuple[str, ...] = (
    # 001 — core entities
    """
    CREATE TABLE app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE libraries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plex_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        section_type TEXT NOT NULL,
        protected INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE media_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER REFERENCES libraries(id) ON DELETE CASCADE,
        plex_rating_key TEXT NOT NULL UNIQUE,
        parent_rating_key TEXT,
        title TEXT NOT NULL,
        item_type TEXT NOT NULL CHECK (item_type IN ('series', 'season', 'movie')),
        season_number INTEGER,
        added_at TEXT,
        last_played_at TEXT,
        rating REAL,
        rating_source TEXT,
        production_status TEXT,
        file_path TEXT,
        file_size_bytes INTEGER,
        disk TEXT,
        hardlink_count INTEGER,
        sonarr_id INTEGER,
        radarr_id INTEGER,
        monitored INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_media_items_library ON media_items(library_id);
    CREATE INDEX idx_media_items_type ON media_items(item_type);
    CREATE INDEX idx_media_items_parent ON media_items(parent_rating_key);

    CREATE TABLE episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_item_id INTEGER REFERENCES media_items(id) ON DELETE CASCADE,
        plex_rating_key TEXT NOT NULL UNIQUE,
        series_rating_key TEXT NOT NULL,
        season_number INTEGER,
        episode_number INTEGER,
        title TEXT,
        file_path TEXT,
        file_size_bytes INTEGER,
        added_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_episodes_series ON episodes(series_rating_key);

    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plex_user_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        is_owner INTEGER NOT NULL DEFAULT 0,
        excluded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE play_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        plex_rating_key TEXT NOT NULL,
        watched INTEGER NOT NULL DEFAULT 0,
        played_at TEXT,
        position_ms INTEGER,
        duration_ms INTEGER,
        source TEXT NOT NULL DEFAULT 'plex',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_play_events_item ON play_events(plex_rating_key);
    CREATE INDEX idx_play_events_user ON play_events(user_id);

    CREATE TABLE scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        mode TEXT NOT NULL DEFAULT 'dry_run',
        candidates_found INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );

    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        reason TEXT NOT NULL,
        facts TEXT,
        state TEXT NOT NULL DEFAULT 'candidate',
        detected_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_candidates_item ON candidates(media_item_id);

    CREATE TABLE protections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('library', 'rating', 'keep_forever')),
        library_id INTEGER REFERENCES libraries(id) ON DELETE CASCADE,
        media_item_id INTEGER REFERENCES media_items(id) ON DELETE CASCADE,
        detail TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE removal_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
        candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
        category TEXT NOT NULL,
        reason TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'marked',
        marked_at TEXT NOT NULL DEFAULT (datetime('now')),
        marked_by TEXT NOT NULL DEFAULT 'owner',
        grace_days INTEGER NOT NULL,
        due_at TEXT NOT NULL,
        collection_added INTEGER NOT NULL DEFAULT 0,
        recheck_status TEXT,
        cancel_reason TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_removal_requests_state ON removal_requests(state);

    CREATE TABLE removal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        removal_request_id INTEGER REFERENCES removal_requests(id) ON DELETE SET NULL,
        media_item_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('deleted', 'cancelled', 'failed')),
        title TEXT NOT NULL,
        unit TEXT NOT NULL,
        category TEXT,
        reason TEXT,
        size_bytes INTEGER,
        freed_bytes INTEGER,
        disk TEXT,
        marked_by TEXT,
        error TEXT,
        occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX idx_removal_events_occurred ON removal_events(occurred_at);
    """,
    # 002 — idempotent play events
    """
    CREATE UNIQUE INDEX idx_play_events_unique
        ON play_events(user_id, plex_rating_key, played_at, position_ms);
    """,
    # 003 — verified deletion state and diagnostic (no credentials)
    """
    ALTER TABLE removal_requests ADD COLUMN last_error TEXT;
    ALTER TABLE removal_requests ADD COLUMN deleted_at TEXT;
    """,
    # 004 — external ids that link Plex items to Radarr/Sonarr, and the
    # stored report of every scan run (what ran or what would have run)
    """
    ALTER TABLE media_items ADD COLUMN tmdb_id INTEGER;
    ALTER TABLE media_items ADD COLUMN tvdb_id INTEGER;
    ALTER TABLE media_items ADD COLUMN imdb_id TEXT;
    ALTER TABLE scan_runs ADD COLUMN trigger TEXT;
    ALTER TABLE scan_runs ADD COLUMN report TEXT;
    """,
    # 005 — the release year for the title+year link fallback, and the
    # per-episode file facts (disk, hardlink count) the file refresh keeps
    """
    ALTER TABLE media_items ADD COLUMN year INTEGER;
    ALTER TABLE episodes ADD COLUMN disk TEXT;
    ALTER TABLE episodes ADD COLUMN hardlink_count INTEGER;
    """,
    # 006 — indexed lookup for the scheduler's finished daily slot
    """
    CREATE INDEX idx_scan_runs_schedule_slot
        ON scan_runs(trigger, status, unixepoch(started_at));
    """,
    # 007 — indexed lookup for the protections of one media item
    """
    CREATE INDEX idx_protections_item
        ON protections(media_item_id);
    """,
    # 008 — indexed order for the History page: the events sorted by the
    # instant they happened, so the index serves that order
    """
    CREATE INDEX idx_removal_events_instant
        ON removal_events(julianday(occurred_at), id);
    """,
    # 009 — soft Plex presence. Legacy rows remain eligible until a complete
    # section read proves absence; rows and their history are never removed.
    """
    ALTER TABLE media_items ADD COLUMN plex_present INTEGER NOT NULL DEFAULT 1
        CHECK (plex_present IN (0, 1));
    ALTER TABLE episodes ADD COLUMN plex_present INTEGER NOT NULL DEFAULT 1
        CHECK (plex_present IN (0, 1));
    """,
    # 010 — materialized page projections and bounded full reports. Triggers
    # also cover imports/repairs that write scan_runs directly.
    f"""
    CREATE TABLE scan_run_views (
        run_id INTEGER PRIMARY KEY REFERENCES scan_runs(id) ON DELETE CASCADE,
        snapshot TEXT NOT NULL,
        steps TEXT NOT NULL
    );
    INSERT INTO scan_run_views {_SCAN_VIEW_SELECT}
        WHERE id IN (SELECT id FROM scan_runs ORDER BY id DESC LIMIT {SCAN_REPORT_RETENTION})
           OR id = (SELECT MAX(id) FROM scan_runs WHERE status = 'finished');
    UPDATE scan_runs SET report = NULL WHERE id NOT IN
        (SELECT id FROM scan_runs ORDER BY id DESC LIMIT {SCAN_REPORT_RETENTION});
    CREATE TRIGGER scan_run_view_insert AFTER INSERT ON scan_runs BEGIN
        INSERT OR REPLACE INTO scan_run_views {_SCAN_VIEW_SELECT} WHERE id = NEW.id;
    END;
    CREATE TRIGGER scan_run_view_update AFTER UPDATE OF report ON scan_runs
        WHEN NEW.report IS NOT NULL BEGIN
        INSERT OR REPLACE INTO scan_run_views {_SCAN_VIEW_SELECT} WHERE id = NEW.id;
    END;
    """,
    # 011 — the display projection keeps every section of the run's detection
    # counts, so no section the page could read is silently lost. The runs
    # whose full report was already pruned keep their materialized snapshot:
    # their report is gone, and their display must not be.
    f"""
    DROP TRIGGER IF EXISTS scan_run_view_insert;
    DROP TRIGGER IF EXISTS scan_run_view_update;
    UPDATE scan_run_views SET snapshot = (
        SELECT {_SCAN_VIEW_SNAPSHOT}
        FROM (SELECT id, {_SCAN_VIEW_BODY} AS body FROM scan_runs)
        WHERE id = scan_run_views.run_id)
        WHERE run_id IN (SELECT id FROM scan_runs WHERE report IS NOT NULL);
    CREATE TRIGGER scan_run_view_insert AFTER INSERT ON scan_runs BEGIN
        INSERT OR REPLACE INTO scan_run_views {_SCAN_VIEW_SELECT} WHERE id = NEW.id;
    END;
    CREATE TRIGGER scan_run_view_update AFTER UPDATE OF report ON scan_runs
        WHEN NEW.report IS NOT NULL BEGIN
        INSERT OR REPLACE INTO scan_run_views {_SCAN_VIEW_SELECT} WHERE id = NEW.id;
    END;
    """,
    # 012 — the arr side effects a removal applied, recorded as they are
    # applied so a cancellation can undo them (#69). NULL means nothing
    # is applied; the JSON list keeps only the fields that apply.
    """
    ALTER TABLE removal_requests ADD COLUMN applied_effects TEXT;
    """,
    # 013 — #47: the reasons a request and its events carry, as a key plus
    # raw parameters, so every screen renders them in the reader's language.
    # The text columns stay beside them, so older rows and a downgrade keep
    # reading; NULL marks a row written before this migration, text only.
    """
    ALTER TABLE removal_requests ADD COLUMN reason_parts TEXT;
    ALTER TABLE removal_requests ADD COLUMN cancel_params TEXT;
    ALTER TABLE removal_events ADD COLUMN reason_parts TEXT;
    """,
    # 014 — #48: the Library reads one page of typed rows instead of the whole
    # snapshot, and counts and filters in SQL. The backfill fills the cache on
    # upgrade from the snapshot that is already published, so an upgraded
    # database shows its latest scan immediately; the triggers then keep the
    # cache in step with every later write of that snapshot.
    f"""
    CREATE TABLE scan_rows (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('candidate', 'protected')),
        position INTEGER NOT NULL,
        media_item_id INTEGER NOT NULL,
        unit TEXT NOT NULL,
        seasons TEXT NOT NULL,
        title TEXT NOT NULL,
        type_badge TEXT NOT NULL,
        categories TEXT NOT NULL,
        reasons TEXT NOT NULL,
        reason TEXT NOT NULL,
        reason_parts TEXT,
        size_bytes INTEGER NOT NULL,
        disk TEXT,
        last_played_at TEXT,
        rating REAL,
        rating_source TEXT,
        protection_kind TEXT,
        protection_reason TEXT,
        protection_reason_parts TEXT
    );
    CREATE TABLE scan_row_categories (
        scan_row_id INTEGER NOT NULL REFERENCES scan_rows(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        category TEXT NOT NULL,
        PRIMARY KEY (scan_row_id, category)
    );
    CREATE INDEX idx_scan_rows_sort
        ON scan_rows(kind, size_bytes DESC, title, media_item_id);
    CREATE INDEX idx_scan_rows_item ON scan_rows(kind, media_item_id);
    CREATE INDEX idx_scan_row_categories_filter
        ON scan_row_categories(kind, category, scan_row_id);
    {_scan_rows_publish('(SELECT snapshot FROM scan_run_views WHERE run_id = ' + _LATEST_FINISHED_RUN + ')', _LATEST_FINISHED_RUN)}
    CREATE TRIGGER scan_rows_publish_insert AFTER INSERT ON scan_run_views
    WHEN NEW.run_id = {_LATEST_FINISHED_RUN} BEGIN
        {_scan_rows_publish('NEW.snapshot', 'NEW.run_id')}
    END;
    CREATE TRIGGER scan_rows_publish_update AFTER UPDATE ON scan_run_views
    WHEN NEW.run_id = {_LATEST_FINISHED_RUN} BEGIN
        {_scan_rows_publish('NEW.snapshot', 'NEW.run_id')}
    END;
    """,
    # 015 — #74: the TMDB answers detection reuses across scans (with a TTL),
    # and the two indexes the pages read through: the requests of one item,
    # and the oldest imported Tautulli event the sources panel shows.
    """
    CREATE TABLE tmdb_cache (
        item_type TEXT NOT NULL CHECK (item_type IN ('movie', 'series')),
        tmdb_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (item_type, tmdb_id)
    );
    CREATE INDEX idx_removal_requests_item ON removal_requests(media_item_id);
    CREATE INDEX idx_play_events_source_played_at ON play_events(source, played_at);
    """,
)


class SchemaVersionError(RuntimeError):
    """Raised when the database schema is newer than this build supports."""


def _private_database_files(path: Path) -> None:
    """Restrict the database and any SQLite sidecars to their owner.

    Best effort: a file owned by another user (an older install created as
    root, say) keeps its mode rather than stopping the app from opening it.
    """
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            os.chmod(candidate, 0o600)
        except OSError:
            continue


def _create_private_database(path: Path) -> None:
    """Create the database file owner-only when it does not exist yet.

    An existing database is never opened read-write here: a read-only file (or
    a read-only location) still has to open for queries, as SQLite allows.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    finally:
        os.close(fd)


def _schema_version(conn: sqlite3.Connection) -> int:
    """Read the database's schema version without writing anything."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _newer_schema_error(current: int) -> SchemaVersionError:
    """Build the refusal for a schema this build cannot understand."""
    return SchemaVersionError(
        f"database schema version {current} is newer than this build "
        f"supports ({SCHEMA_VERSION}); upgrade the app before opening it"
    )


def _prune_backups(backup_dir: Path, prefix: str) -> None:
    """Keep only the most recent backups, oldest first on the chopping block."""
    backups = sorted(
        candidate
        for candidate in backup_dir.glob(f"{prefix}-*.db")
        if candidate.is_file()
    )
    for stale in backups[:-BACKUP_RETENTION] if BACKUP_RETENTION > 0 else backups:
        try:
            stale.unlink()
        except OSError:
            continue  # best effort: an undeletable copy never stops a migration


def _create_backup_file(backup_dir: Path, stem: str, stamp: str) -> Path:
    """Create an owner-only backup file and return its path.

    Two copies in the same second must not collide: the second one gets its
    own name instead of stopping the migration.
    """
    for attempt in range(100):
        name = f"{stem}-{stamp}.db" if attempt == 0 else f"{stem}-{stamp}-{attempt}.db"
        candidate = backup_dir / name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError(f"no free backup name in {backup_dir} for {stamp}")


def _backup_before_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Copy the database to <config dir>/backups before migrating it.

    Only a database with a migration actually pending (0 < schema version <
    SCHEMA_VERSION) is copied: a new database has nothing to protect and an
    up-to-date one has nothing to migrate, and neither should be copied on
    every startup. The copy uses SQLite's online backup API (not a file
    copy) because the database runs in WAL mode and the WAL file holds
    committed content a plain copy can miss. Fails closed: an unwritable
    backup stops the migration.
    """
    current = _schema_version(conn)
    if not 0 < current < SCHEMA_VERSION or not path.exists():
        return
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # owner-only from the first byte, not chmod'ed after the copy lands
    backup_path = _create_backup_file(backup_dir, path.stem, stamp)
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    _prune_backups(backup_dir, path.stem)


def meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    """One durable key/value fact stored in ``app_meta``.

    The history cursors #74 kept here are no longer read or written: every
    scan reads the whole history (#112). A missing key answers ``None``.
    """
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Store one durable key/value fact in ``app_meta``.

    The caller owns the transaction, so the fact commits with the rows it
    describes.
    """
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys, WAL and the #110 read settings."""
    path = Path(database_path)
    is_memory_database = str(database_path) == ":memory:"
    if not is_memory_database:
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_database(path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    current = _schema_version(conn)
    if current > SCHEMA_VERSION:
        conn.close()
        raise _newer_schema_error(current)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # a read-only database keeps the journal mode it has
    # #110: NORMAL is durable only under WAL (or for a database that lives in
    # memory); a database SQLite left on a rollback journal keeps FULL, since
    # NORMAL there can corrupt it on a power loss. The switch can fail quietly
    # (it answers with the mode it kept), so the mode is read back.
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode in ("wal", "memory"):
        conn.execute("PRAGMA synchronous = " + SYNCHRONOUS)
    # A 16 MiB page cache keeps the pages' repeated index reads in memory
    # instead of on the file system.
    conn.execute(f"PRAGMA cache_size = -{CACHE_SIZE_KIB}")
    if not is_memory_database:
        _private_database_files(path)
    return conn


def _migration_statements(script: str) -> list[str]:
    """Split one migration script into complete SQL statements.

    Statements end at a semicolon that closes them (``sqlite3`` sees
    trailing text inside a trigger body, say, as part of the statement), so
    a script is never handed to ``execute`` whole.
    """
    statements: list[str] = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration and return the resulting schema version.

    Each migration and its version bump commit as one transaction, so an
    interrupted migration leaves the previous schema and ``user_version``
    intact. A database whose schema version is newer than this build
    supports is refused instead of opened.
    """
    current = _schema_version(conn)
    if current > SCHEMA_VERSION:
        raise _newer_schema_error(current)
    if current == SCHEMA_VERSION:
        # Nothing to apply: the caller's transaction mode and any work it
        # has not committed yet stay exactly as they were.
        return current
    if conn.in_transaction:
        # Switching to explicit transactions would commit the caller's
        # pending work as a side effect; refuse instead of committing it.
        raise sqlite3.ProgrammingError(
            "migrate cannot run inside an open transaction"
        )
    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None  # explicit transactions, not implicit ones
    try:
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _migration_statements(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # the transaction never opened or already ended
                raise
            current = version
    finally:
        conn.isolation_level = previous_isolation_level
    return current


def init_db(database_path: str | Path) -> sqlite3.Connection:
    """Open the database and make sure the schema is up to date."""
    conn = connect(database_path)
    try:
        if str(database_path) != ":memory:":
            _backup_before_migration(conn, Path(database_path))
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    if str(database_path) != ":memory:":
        _private_database_files(Path(database_path))
    return conn
