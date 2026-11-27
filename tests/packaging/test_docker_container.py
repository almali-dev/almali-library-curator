"""Opt-in container test (T12, S8): build the image, run it, check the runtime contract.

Runs only with ``ALC_DOCKER_TESTS=1`` and a reachable Docker daemon; it is
skipped cleanly otherwise. The image and the container are removed afterwards.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUID, PGID = 1234, 1235
# #71: the run options the installer and the compose example use. Every test
# in this module runs the container this way, so the hardening is proved by the
# same evidence as the rest of the runtime contract (the entrypoint still fixes
# /config and drops to PUID:PGID with only these capabilities, Gunicorn starts
# with a read-only root and a /tmp tmpfs).
HARDENED_RUN_OPTIONS = [
    "--cap-drop", "ALL",
    "--cap-add", "CHOWN",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges:true",
    "--read-only",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
]


def _docker_available() -> bool:
    if os.environ.get("ALC_DOCKER_TESTS") != "1" or shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="set ALC_DOCKER_TESTS=1 with a reachable Docker daemon"
)


def _docker(*args: str, timeout: float = 60) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def container():
    suffix = uuid.uuid4().hex[:10]
    image = f"almali-library-curator-test:{suffix}"
    name = f"alc-test-{suffix}"
    _docker("build", "--pull=false", "-t", image, str(ROOT), timeout=900)
    try:
        _docker(
            "run", "-d", "--name", name, *HARDENED_RUN_OPTIONS,
            "-e", f"PUID={PUID}", "-e", f"PGID={PGID}",
            "-e", "ALC_AUTH_USER=owner", "-e", "ALC_AUTH_PASSWORD=container-test-password",
            "-p", "127.0.0.1::17540",
            image,
        )
        yield {"image": image, "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def _health(name: str) -> str:
    return _docker("inspect", "--format", "{{.State.Health.Status}}", name)


def test_container_becomes_healthy(container):
    deadline = time.monotonic() + 120
    status = ""
    while time.monotonic() < deadline:
        status = _health(container["name"])
        if status in {"healthy", "unhealthy"}:
            break
        time.sleep(2)
    assert status == "healthy", _docker("logs", container["name"])


def test_healthz_answers_ok_without_plex(container):
    test_container_becomes_healthy(container)
    host_port = _docker("port", container["name"], "17540/tcp").splitlines()[0]
    with urllib.request.urlopen(f"http://{host_port}/healthz", timeout=10) as response:
        assert response.status == 200
        payload = json.loads(response.read())
    assert payload["status"] == "ok"
    assert payload["missing"] == ["plex"]


def test_the_login_page_is_served_with_the_owner_login(container):
    test_container_becomes_healthy(container)
    host_port = _docker("port", container["name"], "17540/tcp").splitlines()[0]
    with urllib.request.urlopen(f"http://{host_port}/login", timeout=10) as response:
        assert response.status == 200
        assert 'name="csrf_token"' in response.read().decode()


def test_image_runs_gunicorn_27(container):
    """S11: Gunicorn 27, the locked version, is the WSGI server."""
    version = _docker(
        "exec", "-u", f"{PUID}:{PGID}", container["name"], "python", "-c",
        "import importlib.metadata as m; print(m.version('gunicorn'))",
    )
    assert version.split(".")[0] == "27", version


def test_every_process_runs_as_puid_pgid(container):
    test_container_becomes_healthy(container)
    script = (
        "import os,glob\n"
        "for s in glob.glob('/proc/[0-9]*/status'):\n"
        "    try: lines=open(s).read().splitlines()\n"
        "    except OSError: continue\n"
        "    f={l.split(':')[0]:l.split(':',1)[1].split() for l in lines if ':' in l}\n"
        "    if s.split('/')[2]!=str(os.getpid()): print(f['Name'][0],f['Uid'][0],f['Gid'][0],f['Groups'])\n"
    )
    # Run the probe with the same ids, so the probe itself is not a root process.
    output = _docker("exec", "-u", f"{PUID}:{PGID}", container["name"], "python", "-c", script)
    rows = [line.split(maxsplit=3) for line in output.splitlines()]
    gunicorn = [row for row in rows if row[0].startswith("gunicorn")]
    assert gunicorn, output
    for row in rows:
        assert row[1:3] == [str(PUID), str(PGID)], output
        assert row[3] == "[]", output


def test_config_files_belong_to_puid_pgid(container):
    test_container_becomes_healthy(container)
    output = _docker(
        "exec", container["name"], "find", "/config", "-printf", "%u:%g %p\n"
    )
    entries = output.splitlines()
    names = {line.split(" ", 1)[1] for line in entries}
    assert {"/config", "/config/settings.json", "/config/secret_key"} <= names, output
    assert any(name.endswith(".db") for name in names), output
    for line in entries:
        assert line.split(" ", 1)[0] == f"{PUID}:{PGID}", output


def test_the_manifest_stays_root_owned_while_everything_else_is_handed_over(container):
    """#146: the entrypoint keeps the manifest a root-run host reads root-owned.

    Ownership is not a lock (the container owns /config); the installer and the
    uninstaller validate every path in it before use.

    The installer records host paths in /config/install-manifest.json, and the
    entrypoint runs as root before dropping to PUID:PGID. The manifest must
    keep its root owner and its mode; every other file in /config is still
    handed to PUID:PGID.
    """
    name = container["name"]
    manifest = "/config/install-manifest.json"
    probe = "/config/entrypoint-probe.txt"
    setup = (f": > {manifest}; chmod 600 {manifest}; chown 0:0 {manifest}; "
             f": > {probe}; chmod 644 {probe}; chown 0:0 {probe}")
    try:
        _docker("exec", "-u", "0", name, "sh", "-c", setup)
        _docker("exec", "-u", "0", name, "/usr/local/bin/entrypoint.sh", "true")
        # %U:%G is always numeric; %u:%g prints names for users that exist
        # (root:root) and numbers for the others (PUID:PGID).
        output = _docker("exec", "-u", "0", name, "find", "/config",
                         "-printf", "%U:%G %m %p\\n")
    finally:
        subprocess.run(["docker", "exec", "-u", "0", name, "rm", "-f", manifest, probe],
                       capture_output=True)
    owners = {}
    for line in output.splitlines():
        owner, mode, path = line.split(" ", 2)
        owners[path] = (owner, mode)
    assert owners[manifest] == ("0:0", "600"), output
    assert owners[probe] == (f"{PUID}:{PGID}", "644"), output


def test_fresh_install_starts_in_dry_run(container):
    test_container_becomes_healthy(container)
    stored = json.loads(_docker("exec", container["name"], "cat", "/config/settings.json"))
    assert stored["dry_run"] is True
    resolved = _docker(
        "exec", "-u", f"{PUID}:{PGID}", container["name"], "python", "-c",
        "from almali_library_curator.config import load_settings; print(load_settings().dry_run)",
    )
    assert resolved == "True"
    logs = subprocess.run(
        ["docker", "logs", container["name"]], capture_output=True, text=True
    )
    assert "application_started" in logs.stdout + logs.stderr
