"""Contracts for the reusable CI gate used by branch CI and releases."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is not installed")
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_ci_covers_the_release_branch_tags_and_pull_requests(workflow):
    triggers = workflow.get("on", workflow.get(True))
    assert {"main", "feature/almali-library-curator-v1"} <= set(triggers["push"]["branches"])
    assert triggers["push"]["tags"] == ["v*"]
    assert "pull_request" in triggers


def test_ci_is_reusable_and_read_only(workflow):
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    assert workflow["permissions"] == {"contents": "read"}


def test_ci_tests_all_supported_pythons_from_committed_locks(workflow):
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]
    setup = next(s for s in job["steps"] if "setup-python@" in s.get("uses", ""))
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    install = next(s["run"] for s in job["steps"] if s.get("name") == "Install")
    assert "--require-hashes -r docker/requirements.lock" in install
    assert "--require-hashes -r docker/build-requirements.lock" in install
    assert '--no-build-isolation -e ".[dev,browser]"' in install
    # Existing behavior: pytest still runs the complete test discovery, not a subset.
    assert any(s.get("run") == "python -m pytest" for s in job["steps"])


def test_ci_audits_the_locked_runtime(workflow):
    audit = workflow["jobs"]["audit"]
    commands = "\n".join(s.get("run", "") for s in audit["steps"])
    assert "pip-audit" in commands
    assert "-r docker/requirements.lock" in commands
    assert "--disable-pip" in commands


def test_ci_builds_and_boots_the_image_without_silent_skips(workflow):
    job = workflow["jobs"]["docker"]
    assert job["env"]["ALC_DOCKER_TESTS"] == "1"
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "docker info" in commands
    assert "python -m pytest tests/packaging/test_docker_container.py" in commands


def test_ci_docker_job_installs_what_the_shared_conftest_imports(workflow):
    # tests/conftest.py imports Flask and the package, so pytest alone cannot
    # even collect the container test (run 43614261293 failed this way).
    job = workflow["jobs"]["docker"]
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "--require-hashes -r docker/requirements.lock" in commands
    assert '-e ".[dev]"' in commands


def test_ci_requires_optional_tests_and_installs_chromium(workflow):
    job = workflow["jobs"]["test"]
    assert job["env"]["ALC_REQUIRE_OPTIONAL_TESTS"] == "1"
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'pip install --no-build-isolation -e ".[dev,browser]"' in commands
    assert "python -m playwright install --with-deps chromium" in commands


def test_ci_runs_lint_and_type_gates(workflow):
    commands = "\n".join(s.get("run", "") for s in workflow["jobs"]["test"]["steps"])
    assert "python -m ruff check --no-cache ." in commands
    assert "python -m pyright" in commands


def test_dev_and_browser_extras_pin_the_gate_tools():
    import tomllib

    project = tomllib.loads((WORKFLOW.parents[2] / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    assert {"pytest", "PyYAML", "pytest-xdist", "ruff", "pyright"} <= {
        requirement.split("==")[0] for requirement in extras["dev"]
    }
    assert all("==" in requirement for requirement in extras["dev"])
    assert len(extras["browser"]) == 1
    assert extras["browser"][0].startswith("playwright==")


def test_every_action_is_pinned_to_a_commit(workflow):
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"actions/[^@]+@[0-9a-f]{40}", step["uses"])
    uses_lines = [line for line in WORKFLOW.read_text().splitlines() if "uses:" in line]
    assert all(re.search(r" # v\d+(?:\.\d+){0,2}$", line) for line in uses_lines)
