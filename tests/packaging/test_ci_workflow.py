"""Tests of the continuous-integration workflow."""
from __future__ import annotations

from pathlib import Path
import re

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is not installed")

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow():
    assert WORKFLOW.is_file(), ".github/workflows/ci.yml is missing"
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def steps(workflow) -> list[dict]:
    jobs = workflow.get("jobs", {})
    collected: list[dict] = []
    for job in jobs.values():
        collected.extend(job.get("steps", []))
    return collected


def test_every_action_is_pinned_to_a_commit(workflow):
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    raw_uses = re.findall(
        r"^\s*(?:-\s*)?uses:\s*(?P<action>[^@\s]+)@(?P<sha>[0-9a-f]{40})"
        r"(?:\s+#\s*(?P<version>v\d+))?\s*$",
        workflow_text,
        re.MULTILINE,
    )
    parsed_uses = [
        step["uses"] for step in steps(workflow) if step.get("uses") is not None
    ]
    assert len(raw_uses) == len(parsed_uses), "every uses: must name a full commit SHA"

    for (action, sha, version), uses in zip(raw_uses, parsed_uses):
        assert action.startswith("actions/"), uses
        assert uses == f"{action}@{sha}"
        assert version is not None, f"{uses} is missing its trailing major-version comment"
