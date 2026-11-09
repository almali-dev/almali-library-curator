from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_with_permissive_umask(tmp_path: Path, program: str) -> dict:
    temporary_directory = tmp_path / "temporary"
    temporary_directory.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "config")],
        cwd=Path(__file__).parents[1],
        env={
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(temporary_directory),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_settings_writes_are_private_and_preserve_data_under_a_permissive_umask(tmp_path):
    observed = _run_with_permissive_umask(
        tmp_path,
        """
import json
import os
import stat
import sys

from almali_library_curator import config
from almali_library_curator.config import load_settings, save_settings, update_settings_file

config_dir = sys.argv[1]
os.umask(0)
settings = load_settings(config_dir, env={})
path = save_settings(settings)
update_settings_file(config_dir, {"grace_days": 21})
before_failure = path.read_bytes()
original_replace = config.os.replace
try:
    config.os.replace = lambda source, target: (_ for _ in ()).throw(OSError("replace failed"))
    try:
        update_settings_file(config_dir, {"grace_days": 30})
    except OSError:
        pass
    else:
        raise AssertionError("the simulated replacement failure must be raised")
finally:
    config.os.replace = original_replace
unchanged_after_failure = path.read_bytes() == before_failure

reopened = load_settings(config_dir, env={"ALC_DRY_RUN": "0"})
update_settings_file(config_dir, {"collection_name": "Private library"})
stored = json.loads(path.read_text(encoding="utf-8"))
print(json.dumps({
    "mode": stat.S_IMODE(path.stat().st_mode),
    "stored_grace_days": stored["grace_days"],
    "stored_dry_run": stored["dry_run"],
    "stored_collection_name": stored["collection_name"],
    "reopened_dry_run": reopened.dry_run,
    "unchanged_after_failure": unchanged_after_failure,
    "temporary_files": [item.name for item in path.parent.glob(".settings.json.*")],
}))
""",
    )

    assert observed == {
        "mode": 0o600,
        "stored_grace_days": 21,
        "stored_dry_run": True,
        "stored_collection_name": "Private library",
        "reopened_dry_run": False,
        "unchanged_after_failure": True,
        "temporary_files": [],
    }


def test_database_and_sqlite_sidecars_are_private_under_a_permissive_umask(tmp_path):
    observed = _run_with_permissive_umask(
        tmp_path,
        """
import json
import os
import stat
import sys
from pathlib import Path

from almali_library_curator.db import connect, init_db

path = Path(sys.argv[1]) / "almali-library-curator.db"
os.umask(0)
conn = init_db(path)
conn.execute("INSERT INTO app_meta (key, value) VALUES ('private', 'preserved')")
conn.commit()
conn.close()
path.chmod(0o666)
reopened = connect(path)
try:
    value = reopened.execute("SELECT value FROM app_meta WHERE key = 'private'").fetchone()[0]
    sidecars = [candidate for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")) if candidate.exists()]
    modes = {candidate.name: stat.S_IMODE(candidate.stat().st_mode) for candidate in [path, *sidecars]}
finally:
    reopened.close()
memory = connect(":memory:")
try:
    memory.execute("CREATE TABLE memory_only (value TEXT)")
    memory.execute("INSERT INTO memory_only VALUES ('kept in memory')")
    memory_value = memory.execute("SELECT value FROM memory_only").fetchone()[0]
finally:
    memory.close()
print(json.dumps({"modes": modes, "value": value, "memory_value": memory_value}))
""",
    )

    assert observed["modes"]["almali-library-curator.db"] == 0o600
    assert all(mode == 0o600 for mode in observed["modes"].values())
    assert observed["value"] == "preserved"
    assert observed["memory_value"] == "kept in memory"


def test_a_database_owned_by_another_user_still_opens(tmp_path, monkeypatch):
    from almali_library_curator import db

    path = tmp_path / "config" / "almali-library-curator.db"
    db.init_db(path).close()

    def refuse(*_args, **_kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(db.os, "chmod", refuse)
    monkeypatch.setattr(db.os, "fchmod", refuse)
    conn = db.init_db(path)
    try:
        assert conn.execute("SELECT count(*) FROM app_meta").fetchone()[0] >= 0
    finally:
        conn.close()
