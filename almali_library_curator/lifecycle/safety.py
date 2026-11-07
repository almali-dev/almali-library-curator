"""Immediate deletion controls shared by every process."""
import sqlite3


def kill_switch_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'kill_switch'").fetchone()
    # Unknown stored values fail closed; only an explicit off disables it.
    return row is not None and row["value"] != "off"


def set_kill_switch(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES ('kill_switch', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("on" if on else "off",),
    )
    conn.commit()
