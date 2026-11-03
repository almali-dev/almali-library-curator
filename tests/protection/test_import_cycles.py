"""The detection engine and the protection package import cleanly in
both directions: neither import order may create a cycle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def import_in_a_fresh_process(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_protection_package_imports_on_its_own():
    result = import_in_a_fresh_process("almali_library_curator.protection")
    assert result.returncode == 0, result.stderr


def test_detection_engine_imports_on_its_own():
    result = import_in_a_fresh_process("almali_library_curator.detection.engine")
    assert result.returncode == 0, result.stderr
