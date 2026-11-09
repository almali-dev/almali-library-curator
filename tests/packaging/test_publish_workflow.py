"""Tests of the image publish workflow (T13, S8).

The workflow is the only outward-facing piece of the delivery: it pushes to
GHCR and signs the image. It must never run on a pull request or on a branch
push (L109 decision 1), and it must publish a signed multi-arch image.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is not installed")

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"
DEPENDABOT = Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml"
IMAGE = "ghcr.io/almali-dev/almali-library-curator"
TAG_ONLY = "${{ startsWith(github.ref, 'refs/tags/v') }}"


@pytest.fixture(scope="module")
def workflow():
    assert WORKFLOW.is_file(), ".github/workflows/publish.yml is missing"
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def triggers(workflow):
    # YAML 1.1 reads the key "on" as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow) -> list[dict]:
    jobs = workflow.get("jobs", {})
    collected: list[dict] = []
    for job in jobs.values():
        collected.extend(job.get("steps", []))
    return collected


def test_the_workflow_parses(workflow):
    assert workflow.get("jobs")


def test_the_workflow_runs_only_on_tags_and_on_demand(triggers):
    assert "pull_request" not in triggers
    push = triggers["push"]
    assert "branches" not in push
    assert push["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers


def test_the_workflow_has_the_permissions_the_registry_and_cosign_need(workflow):
    permissions = workflow["permissions"]
    assert permissions["contents"] == "read"
    assert permissions["packages"] == "write"
    assert permissions["id-token"] == "write"


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
        assert action.startswith("actions/") or action.startswith("docker/") or action.startswith(
            "sigstore/"
        ), uses
        assert uses == f"{action}@{sha}"
        assert version is not None, f"{uses} is missing its trailing major-version comment"


def test_the_workflow_sets_up_qemu_and_buildx(workflow):
    used = [step.get("uses", "") for step in steps(workflow)]
    assert any(uses.startswith("docker/setup-qemu-action@") for uses in used)
    assert any(uses.startswith("docker/setup-buildx-action@") for uses in used)


def test_the_workflow_logs_in_to_ghcr_with_the_github_token(workflow):
    login = [
        step for step in steps(workflow)
        if step.get("uses", "").startswith("docker/login-action@")
    ]
    assert login, "the workflow never logs in to a registry"
    assert login[0].get("if") == TAG_ONLY
    with_ = login[0]["with"]
    assert with_["registry"] == "ghcr.io"
    assert "GITHUB_TOKEN" in str(with_["password"])


def test_the_workflow_builds_both_architectures(workflow):
    build = [
        step for step in steps(workflow)
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert build, "the workflow never builds the image"
    platforms = str(build[0]["with"]["platforms"])
    assert "linux/amd64" in platforms
    assert "linux/arm64" in platforms
    assert build[0]["with"].get("push") == TAG_ONLY


def test_the_workflow_publishes_the_semver_tag_and_latest_only_from_v_tags(workflow):
    metadata = [
        step for step in steps(workflow)
        if step.get("uses", "").startswith("docker/metadata-action@")
    ]
    assert metadata, "the workflow never computes the image tags"
    with_ = metadata[0]["with"]
    assert IMAGE in str(with_["images"])
    tags = str(with_["tags"])
    assert "type=semver" in tags
    latest_lines = [
        line.strip() for line in tags.splitlines()
        if re.search(r"\bvalue=latest\b", line)
    ]
    assert len(latest_lines) == 1, tags
    assert re.fullmatch(
        r"type=raw\s*,\s*value=latest\s*,\s*enable=\$\{\{\s*startsWith\(\s*"
        r"github\.ref\s*,\s*['\"]refs/tags/v['\"]\s*\)\s*\}\}",
        latest_lines[0],
    ), tags


def test_the_workflow_signs_the_pushed_digest_with_cosign(workflow):
    installer = [
        step for step in steps(workflow)
        if step.get("uses", "").startswith("sigstore/cosign-installer@")
    ]
    assert installer, "cosign is never installed"
    assert installer[0].get("if") == TAG_ONLY
    signing = [
        step for step in steps(workflow)
        if "cosign sign" in str(step.get("run", ""))
    ]
    assert signing, "the workflow never signs the image"
    assert signing[0].get("if") == TAG_ONLY
    run = str(signing[0]["run"])
    assert "--yes" in run
    assert f"{IMAGE}@" in run
    assert "digest" in run.lower()


def test_the_workflow_never_runs_on_a_pull_request(workflow):
    for job in workflow.get("jobs", {}).values():
        assert "pull_request" not in str(job.get("if", ""))


def test_dependabot_updates_github_actions_weekly():
    assert DEPENDABOT.is_file(), ".github/dependabot.yml is missing"
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert data["version"] == 2
    updates = data["updates"]
    actions = [update for update in updates if update["package-ecosystem"] == "github-actions"]
    assert len(actions) == 1
    assert actions[0]["directory"] == "/"
    assert actions[0]["schedule"]["interval"] == "weekly"


def test_dependabot_watches_the_manifests_behind_the_image_locks():
    """T8 (S19): the declared bounds and the lock directory stay current."""
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    pip_updates = [update for update in data["updates"] if update["package-ecosystem"] == "pip"]
    assert pip_updates
    for update in pip_updates:
        assert update["schedule"]["interval"] == "weekly"
    directories = {update["directory"] for update in pip_updates}
    assert "/" in directories, "pyproject.toml declares the bounds the image installs"
    assert "/docker" in directories, "the image locks live in docker/"
