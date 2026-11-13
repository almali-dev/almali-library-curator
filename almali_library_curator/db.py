"""SQLite storage: connection handling and forward-only migrations.

The schema version lives in SQLite's ``user_version`` pragma, so an existing
installation can be migrated in place without a separate bookkeeping table.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 9

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


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys and row access by name."""
    path = Path(database_path)
    is_memory_database = str(database_path) == ":memory:"
    if not is_memory_database:
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_private_database(path)
    conn = sqlite3.connect(path)
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
