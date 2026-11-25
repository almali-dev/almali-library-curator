"""Public workflow contracts and executable, offline release-preflight tests."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import tomllib

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is not installed")
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/publish.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"
IMAGE = "ghcr.io/almali-dev/almali-library-curator"
DIGEST = "sha256:" + "a" * 64
TAG_ONLY = "${{ startsWith(github.ref, 'refs/tags/v') }}"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(workflow):
    return [s for j in workflow["jobs"].values() for s in j.get("steps", [])]


def action(workflow, name):
    return next(s for s in steps(workflow) if s.get("uses", "").startswith(name + "@"))


def test_the_workflow_runs_only_on_tags_and_on_demand(workflow):
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}


def test_publish_depends_on_the_reusable_ci_gate(workflow):
    assert workflow["jobs"]["test"]["uses"] == "./.github/workflows/ci.yml"
    assert workflow["jobs"]["publish"]["needs"] == ["test"]
    assert workflow["jobs"]["publish"]["if"] == TAG_ONLY
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {
        "contents": "read", "packages": "write", "id-token": "write",
    }


def test_all_versions_share_one_non_cancelling_publish_lock(workflow):
    assert workflow["concurrency"] == {
        "group": "${{ startsWith(github.ref, 'refs/tags/v') && 'ghcr-publish' || format('ghcr-check-{0}', github.run_id) }}",
        "cancel-in-progress": False,
    }


def test_every_action_is_pinned_to_a_commit(workflow):
    for step in steps(workflow):
        if "uses" in step:
            assert re.fullmatch(r"(?:actions|docker|sigstore)/[^@]+@[0-9a-f]{40}", step["uses"])
    for line in WORKFLOW.read_text().splitlines():
        if "uses:" in line and "./.github/" not in line:
            assert re.search(r" # v\d+(?:\.\d+){0,2}$", line)


def test_existing_multi_arch_login_and_signing_contracts(workflow):
    action(workflow, "docker/setup-qemu-action")
    action(workflow, "docker/setup-buildx-action")
    login = action(workflow, "docker/login-action")
    assert login["if"] == TAG_ONLY
    assert login["with"]["registry"] == "ghcr.io"
    assert "GITHUB_TOKEN" in login["with"]["password"]
    build = action(workflow, "docker/build-push-action")
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["push"] == TAG_ONLY
    assert action(workflow, "sigstore/cosign-installer")["if"] == TAG_ONLY
    signing = next(s for s in steps(workflow) if "cosign sign" in s.get("run", ""))
    assert signing["if"] == "${{ steps.build.outputs.digest != '' }}"
    assert f"cosign sign --yes {IMAGE}@${{{{ steps.build.outputs.digest }}}}" in signing["run"]


def test_cosign_stays_on_v2_so_existing_verifiers_keep_working(workflow):
    installer = action(workflow, "sigstore/cosign-installer")
    assert installer["with"] == {"cosign-release": "v2.6.5"}


def test_the_installer_blob_is_signed_with_a_bundle_on_tags_only(workflow):
    """#56: the script a release publishes is signed exactly like the image."""
    publish_steps = workflow["jobs"]["publish"]["steps"]
    signer = next(s for s in publish_steps if "cosign sign-blob" in s.get("run", ""))
    assert signer["if"] == TAG_ONLY
    assert "--yes" in signer["run"]
    # cosign v2.6.5 writes the bundle only when it is asked for it.
    assert "--bundle install-unraid.sh.sigstore.json" in signer["run"]
    assert signer["run"].rstrip().endswith("install-unraid.sh")
    assert publish_steps.index(action(workflow, "sigstore/cosign-installer")) < publish_steps.index(signer)
    guard = next(s for s in publish_steps if s.get("id") == "release")
    assert publish_steps.index(guard) < publish_steps.index(signer)


def test_the_installer_bundle_is_published_for_the_exact_version(workflow):
    """#56 L3: no GitHub Release before v1.0.0, so the bundle rides next to the image."""
    publish_steps = workflow["jobs"]["publish"]["steps"]
    signer = next(s for s in publish_steps if "cosign sign-blob" in s.get("run", ""))
    publisher = next(
        s for s in publish_steps
        if "installer-" in s.get("run", "") and s is not signer
    )
    assert publisher["if"] == TAG_ONLY
    assert publisher["env"]["VERSION"] == "${{ github.ref_name }}"
    assert "install-unraid.sh.sigstore.json" in publisher["run"]
    assert f"{IMAGE}:installer-${{VERSION#v}}" in publisher["run"]
    # The bundle is fetched with docker on amd64 and arm64 Unraid hosts alike.
    assert "--platform linux/amd64,linux/arm64" in publisher["run"]
    assert publish_steps.index(signer) < publish_steps.index(publisher)


def test_build_publishes_sbom_and_provenance(workflow):
    build = action(workflow, "docker/build-push-action")["with"]
    assert build["sbom"] is True
    assert build["provenance"] == "mode=max"


def test_preflight_precedes_all_publishing_and_has_full_tag_history(workflow):
    publish_steps = workflow["jobs"]["publish"]["steps"]
    checkout = action(workflow, "actions/checkout")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["fetch-tags"] is True
    guard = next(s for s in publish_steps if s.get("id") == "release")
    assert publish_steps.index(action(workflow, "sigstore/cosign-installer")) < publish_steps.index(guard)
    for name in ("docker/metadata-action", "docker/build-push-action"):
        assert action(workflow, name)["if"] == "${{ steps.release.outputs.tags != '' }}"
    assert guard["shell"] == "python"
    assert guard["env"]["RELEASE_TAG"] == "${{ github.ref_name }}"
    assert publish_steps.index(guard) < publish_steps.index(action(workflow, "docker/build-push-action"))
    meta = action(workflow, "docker/metadata-action")["with"]
    assert meta["tags"] == "${{ steps.release.outputs.tags }}"
    assert meta["flavor"] == "latest=false"
    assert meta["images"] == IMAGE
    assert action(workflow, "docker/build-push-action")["with"]["tags"] == "${{ steps.meta.outputs.tags }}"


def run_preflight(workflow, monkeypatch, tag, version, known=(), registry="absent", signature="signed", digest=DIGEST):
    """Execute the actual workflow step; fake external registry/cosign interfaces.

    No registry writes or filesystem artifacts are possible. Outputs model
    GITHUB_OUTPUT and the subprocess trace detects unexpected side effects.
    """
    guard = next((s for s in steps(workflow) if s.get("id") == "release"), None)
    assert guard is not None, "publish has no release preflight"
    # Run Python's real shell/exit behavior, not an emulation of SystemExit.
    harness = textwrap.dedent('''
        import builtins, contextlib, io, json, os, subprocess, sys, tomllib
        data = json.load(sys.stdin)
        calls = []
        class Output(io.StringIO):
            def close(self):
                pass
        output, stdout = Output(), io.StringIO()
        os.environ["RELEASE_TAG"] = data["tag"]
        os.environ["GITHUB_OUTPUT"] = "workflow-output"
        def run(args, **kwargs):
            calls.append(args)
            if args == ["git", "tag", "--list", "v*"]:
                return subprocess.CompletedProcess(args, 0, "\\n".join(data["known"]), "")
            if args[0] == "cosign":
                target = data["image"] + "@" + data["digest"]
                if args[1] == "sign":
                    assert args == ["cosign", "sign", "--yes", target]
                    return subprocess.CompletedProcess(args, int(data["signature"] == "sign-failed"), "", "signing failed")
                assert args == ["cosign", "verify", "--certificate-identity",
                                "https://github.com/almali-dev/almali-library-curator/.github/workflows/publish.yml@refs/tags/" + data["tag"],
                                "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", target]
                error = {"signed": "", "unsigned": "no signatures found", "foreign": "no matching signatures",
                         "sign-failed": "no signatures found", "denied": "unauthorized", "offline": "connection timed out"}[data["signature"]]
                return subprocess.CompletedProcess(args, int(bool(error)), "[]", error)
            assert args[:5] == ["docker", "buildx", "imagetools", "inspect", data["image"] + ":" + data["tag"][1:]]
            assert args[5:] == ["--format", "{{.Manifest.Digest}}"]
            code, error = {"absent": (1, "ERROR: image: not found"),
                           "present": (0, ""), "denied": (1, "unauthorized"),
                           "offline": (1, "connection timed out")}[data["registry"]]
            return subprocess.CompletedProcess(args, code, data["digest"] if not code else "", error)
        def open_file(path, mode="r", **kwargs):
            if str(path) == "pyproject.toml":
                return io.BytesIO(('[project]\\nversion = "' + data["version"] + '"\\n').encode())
            assert str(path) == "workflow-output" and mode == "a"
            return output
        subprocess.run, builtins.open = run, open_file
        try:
            with contextlib.redirect_stdout(stdout):
                exec(compile(data["script"], "publish-preflight", "exec"), {"__name__": "__main__"})
        finally:
            print(json.dumps({"output": output.getvalue(), "calls": calls, "stdout": stdout.getvalue()}))
    ''')
    result = subprocess.run(
        [sys.executable, "-c", harness], capture_output=True, text=True,
        input=json.dumps(dict(tag=tag, version=version, known=known, registry=registry,
                              signature=signature, digest=digest, image=IMAGE, script=guard["run"])), timeout=10,
    )
    observed = json.loads(result.stdout)
    assert observed["stdout"] == ""
    assert result.returncode in {0, 1}, result.stderr
    if result.returncode == 1:
        assert result.stderr and "Traceback" not in result.stderr
    else:
        assert result.stderr == ""
    return result.stderr or None, observed["output"], observed["calls"]


def test_version_mismatch_refuses_before_registry_or_output(workflow, monkeypatch):
    error, output, calls = run_preflight(workflow, monkeypatch, "v0.32.1", "0.32.0")
    assert error and "version" in str(error).lower()
    assert output == "" and calls == []


def test_existing_version_refuses_without_outputs_or_mutations(workflow, monkeypatch):
    error, output, calls = run_preflight(workflow, monkeypatch, "v0.32.0", "0.32.0", registry="present")
    assert error and "already exists" in str(error)
    assert output == "" and len(calls) == 2


@pytest.mark.parametrize("tag,version", [("v1.0.0", "1.0.0"), ("v1.0.0-rc.1", "1.0.0rc1")])
@pytest.mark.parametrize("signature", ["unsigned", "foreign", "sign-failed", "denied", "offline"])
def test_existing_digest_repair_is_sign_only_and_fails_closed(workflow, monkeypatch, tag, version, signature):
    error, output, calls = run_preflight(workflow, monkeypatch, tag, version, registry="present", signature=signature)
    assert (error is None) == (signature in {"unsigned", "foreign"})
    assert output == ""
    assert [c[:2] for c in calls] == [["docker", "buildx"], ["cosign", "verify"]] + (
        [] if signature in {"denied", "offline"} else [["cosign", "sign"]])


@pytest.mark.parametrize("digest", ["not-a-digest", "", DIGEST + "\nother"])
def test_invalid_existing_digest_refuses_before_cosign(workflow, monkeypatch, digest):
    error, output, calls = run_preflight(workflow, monkeypatch, "v1.0.0", "1.0.0", registry="present", digest=digest)
    assert error and "digest" in error
    assert output == "" and len(calls) == 1


def test_latest_is_only_the_highest_stable_tag(workflow, monkeypatch):
    error, output, _ = run_preflight(workflow, monkeypatch, "v1.2.3", "1.2.3", ["v1.2.3", "v2.0.0"])
    assert error is None
    assert output == "tags<<EOF\ntype=raw,value=1.2.3\ntype=raw,value=1\ntype=raw,value=1.2\nEOF\n"


def test_prerelease_publishes_only_its_immutable_version(workflow, monkeypatch):
    error, output, _ = run_preflight(workflow, monkeypatch, "v2.0.0-rc.1", "2.0.0rc1", ["v1.0.0"])
    assert error is None
    assert output == "tags<<EOF\ntype=raw,value=2.0.0-rc.1\nEOF\n"


def test_highest_stable_gets_latest_major_and_minor(workflow, monkeypatch):
    error, output, _ = run_preflight(workflow, monkeypatch, "v1.10.0", "1.10.0", ["v1.9.9", "v2.0.0-rc.1"])
    assert error is None
    assert output == "tags<<EOF\ntype=raw,value=1.10.0\ntype=raw,value=1\ntype=raw,value=1.10\ntype=raw,value=latest\nEOF\n"


@pytest.mark.parametrize("registry", ["denied", "offline"])
def test_registry_errors_fail_closed(workflow, monkeypatch, registry):
    error, output, calls = run_preflight(workflow, monkeypatch, "v1.0.0", "1.0.0", registry=registry)
    assert error and "registry" in str(error).lower()
    assert output == "" and len(calls) == 1


@pytest.mark.parametrize("tag", ["main", "v01.2.3", "v1.2", "v1.2.3-rc.0\nlatest"])
def test_invalid_tags_fail_without_side_effects(workflow, monkeypatch, tag):
    error, output, calls = run_preflight(workflow, monkeypatch, tag, "1.2.3")
    assert error and "tag" in str(error).lower()
    assert output == "" and calls == []


def test_backfill_does_not_downgrade_major_or_minor(workflow, monkeypatch):
    error, output, _ = run_preflight(workflow, monkeypatch, "v1.2.3", "1.2.3", ["v1.2.4", "v1.3.0"])
    assert error is None
    assert output == "tags<<EOF\ntype=raw,value=1.2.3\nEOF\n"


@pytest.mark.parametrize("version", [None, "1.0.0rc1", "1.0.0a1", "1.0.0b2"])
def test_current_project_version_is_accepted(workflow, monkeypatch, version):
    version = version or tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    tag_version = re.sub(r"(a|b|rc)(\d+)$", lambda m: "-" + {"a": "alpha", "b": "beta", "rc": "rc"}[m[1]] + "." + m[2], version)
    error, output, _ = run_preflight(workflow, monkeypatch, "v" + tag_version, version)
    assert error is None and f"value={tag_version}\n" in output


def test_dependabot_updates_github_actions_weekly():
    data = yaml.safe_load(DEPENDABOT.read_text())
    assert data["version"] == 2
    actions = [u for u in data["updates"] if u["package-ecosystem"] == "github-actions"]
    assert len(actions) == 1 and actions[0]["directory"] == "/"
    assert actions[0]["schedule"]["interval"] == "weekly"


def test_dependabot_watches_the_manifests_behind_the_image_locks():
    data = yaml.safe_load(DEPENDABOT.read_text())
    updates = [u for u in data["updates"] if u["package-ecosystem"] == "pip"]
    assert {"/", "/docker"} <= {u["directory"] for u in updates}
    assert all(u["schedule"]["interval"] == "weekly" for u in updates)


def test_a_released_version_pulls_without_a_login(workflow):
    """#114: after publishing, the exact version pulls anonymously for both architectures."""
    job = workflow["jobs"]["anonymous-pull"]
    assert job["needs"] == ["publish"]
    assert job["if"] == TAG_ONLY
    assert job["permissions"] == {}
    assert not any("uses" in step for step in job["steps"])
    (step,) = job["steps"]
    assert step["env"] == {"VERSION": "${{ github.ref_name }}"}
    script = step["run"]
    assert 'export DOCKER_CONFIG="$(mktemp -d)"' in script
    assert "login" not in script
    for platform in ("linux/amd64", "linux/arm64"):
        assert f'docker pull --platform {platform} "{IMAGE}:${{VERSION#v}}"' in script
