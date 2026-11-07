"""SQLite storage: connection handling and forward-only migrations.

The schema version lives in SQLite's ``user_version`` pragma, so an existing
installation can be migrated in place without a separate bookkeeping table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

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
)


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys and row access by name."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration and return the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
        current = version
    return current


def init_db(database_path: str | Path) -> sqlite3.Connection:
    """Open the database and make sure the schema is up to date."""
    conn = connect(database_path)
    migrate(conn)
    return conn
