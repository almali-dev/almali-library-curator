"""Tests of the Unraid delivery (T13, S8): installer, uninstaller and guide.

The scripts run against a fake ``docker`` executable on ``PATH``, so no real
daemon is touched and the exact ``docker`` command line can be asserted: the
owner password must never appear in it (S17 rule 1: never logged, never on a
command line).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install-unraid.sh"
UNINSTALLER = ROOT / "uninstall-unraid.sh"
GUIDE = ROOT / "docs" / "UNRAID.md"

APP_NAME = "almali-library-curator"
ENV_FILE_NAME = f"{APP_NAME}.env"
MANIFEST_NAME = "install-manifest.json"
SECRET_KEY_NAME = "secret_key"

OWNER = "owner"
PASSWORD = "correct horse battery staple"

# A fake docker: it records every call and never talks to a daemon. It keeps
# a marker per container name, so `docker container inspect` answers as if a
# container created by an earlier call existed.
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG}"
STATE_DIR="${FAKE_DOCKER_STATE}"
mkdir -p "${STATE_DIR}"

# The label a container was created with, read back from the state file.
label_value() {
    local name="$1" key="$2" line value=""
    if [ "${key}" = "ai.almali-dev.managed" ] && [ -n "${FAKE_DOCKER_MANAGED:-}" ]; then
        printf '%s' "${FAKE_DOCKER_MANAGED}"
        return 0
    fi
    if [ -f "${STATE_DIR}/${name}.labels" ]; then
        while IFS= read -r line; do
            case "${line}" in
                "${key}="*) value="${line#*=}" ;;
            esac
        done < "${STATE_DIR}/${name}.labels"
    fi
    printf '%s' "${value}"
}

# The health status the fake container answers, one per `docker inspect
# --format` call: a comma-separated sequence whose last entry repeats once
# it is exhausted ("healthy", "unhealthy", "starting" and then "healthy",
# or "exited" for a container that died).
health_entry() {
    local sequence="${FAKE_DOCKER_HEALTH:-healthy}"
    local counter_file="${STATE_DIR}/.health-counter"
    local counter=0 total=0 index=0
    if [ -f "${counter_file}" ]; then
        counter="$(cat "${counter_file}" 2>/dev/null || printf '0')"
    fi
    printf '%s' "$((counter + 1))" > "${counter_file}"
    total="$(printf '%s' "${sequence}" | awk -v RS=',' 'END {print NR}')"
    index=$((counter + 1))
    if [ "${index}" -gt "${total}" ]; then index="${total}"; fi
    printf '%s' "${sequence}" | awk -v RS=',' -v n="${index}" 'NR==n {print; exit}'
}

case "$1" in
    info|version|pull|ps) exit 0 ;;
    image)
        digests="${FAKE_DOCKER_REPO_DIGESTS:-${FAKE_DOCKER_DIGEST:-ghcr.io/almali-dev/almali-library-curator@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}}"
        if [[ "$*" == *"{{range .RepoDigests}}"* ]]; then
            printf '%s\n' "$digests"
        else
            printf '%s\n' "$digests" | head -n 1
        fi
        exit 0 ;;
    run)
        name=""
        previous=""
        for argument in "$@"; do
            if [ "${previous}" = "--name" ]; then name="${argument}"; fi
            previous="${argument}"
        done
        if [[ "${FAKE_DOCKER_RUN_FAIL:-}" == "1" ]]; then
            echo "docker: port is already allocated" >&2
            exit 1
        fi
        # Another installation takes the name between the conflict check and
        # this run: its container appears, with another attempt's label, and
        # this run is refused.
        if [ -n "${FAKE_DOCKER_RACE_NAME:-}" ] && [ "${name}" = "${FAKE_DOCKER_RACE_NAME}" ]; then
            touch "${STATE_DIR}/${name}"
            printf 'ai.almali-dev.install-attempt=another-installation\n' > "${STATE_DIR}/${name}.labels"
            echo "docker: Conflict. The container name /${name} is already in use" >&2
            exit 1
        fi
        echo "f0f0f0f0f0f0f0f0f0f0"
        [ -n "${name}" ] && touch "${STATE_DIR}/${name}"
        # The labels this container was created with.
        if [ -n "${name}" ]; then
            : > "${STATE_DIR}/${name}.labels"
            previous=""
            for argument in "$@"; do
                if [ "${previous}" = "--label" ]; then
                    printf '%s\n' "${argument}" >> "${STATE_DIR}/${name}.labels"
                fi
                previous="${argument}"
            done
        fi
        exit 0 ;;
    stop|start) exit 0 ;;
    rm|rmi|kill)
        if [ -n "${FAKE_DOCKER_MANIFEST_WATCH:-}" ] && [ -f "${FAKE_DOCKER_MANIFEST_WATCH}" ]; then
            cp "${FAKE_DOCKER_MANIFEST_WATCH}" "${FAKE_DOCKER_MANIFEST_WATCH}.at-rm"
        fi
        if [[ -n "${FAKE_DOCKER_BACKUP_DIR:-}" ]]; then
            backups=("${FAKE_DOCKER_BACKUP_DIR}"/pre-upgrade-*.tar)
            [[ -f "${backups[0]}" ]] || { echo "backup missing before removal" >&2; exit 33; }
        fi
        if [ -n "${FAKE_DOCKER_RM_FAIL:-}" ]; then
            echo "docker: cannot remove container: device or resource busy" >&2
            exit 1
        fi
        for argument in "$@"; do
            rm -f "${STATE_DIR}/${argument}" 2>/dev/null || true
            rm -f "${STATE_DIR}/${argument}.labels" 2>/dev/null || true
        done
        exit 0 ;;
    inspect)
        shift
        if [ -n "${FAKE_DOCKER_INSPECT_SLEEP:-}" ]; then
            sleep "${FAKE_DOCKER_INSPECT_SLEEP}"
        fi
        format=""
        name=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --format|-f) format="${2:-}"; shift 2; continue ;;
            esac
            name="$1"
            shift
        done
        if [ -n "${name}" ] && [ ! -f "${STATE_DIR}/${name}" ]; then
            echo "Error: No such object: ${name}" >&2
            exit 1
        fi
        entry="$(health_entry)"
        case "${format}" in
            "") echo "[]" ;;
            *State.Status*Health.Status*)
                if [ "${entry}" = "exited" ]; then echo "exited";\n                else echo "${FAKE_DOCKER_STATUS:-running} ${entry}"; fi ;;
            *Config.Labels*)
                key="$(printf '%s' "${format}" | sed -n 's/.*Config.Labels[^"]*"\\([^"]*\\)".*/\\1/p')"
                printf '%s\n' "$(label_value "${name}" "${key}")" ;;
            *State.Running*) echo "${FAKE_DOCKER_RUNNING:-true}" ;;
            *Health.Status*)
                if [ "${entry}" = "exited" ]; then echo ""; else echo "${entry}"; fi ;;
            *State.Status*)
                if [ "${entry}" = "exited" ]; then echo "exited"; else echo "${FAKE_DOCKER_STATUS:-running}"; fi ;;
            *) echo "${entry}" ;;
        esac
        exit 0 ;;
    container)
        if [ -f "${STATE_DIR}/${3:-}" ]; then exit 0; fi
        exit 1 ;;
    *) exit 0 ;;
esac
"""

# A fake cosign: it records its arguments and passes or fails on demand
# (FAKE_COSIGN_RESULT=pass|fail).
FAKE_COSIGN = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_COSIGN_LOG}"
case "${FAKE_COSIGN_RESULT:-pass}" in
    fail)
        echo "cosign: the image signature could not be verified" >&2
        exit 1 ;;
    *) exit 0 ;;
esac
"""


@pytest.fixture
def fake_docker(tmp_path):
    """A fake ``docker`` on PATH plus the file it logs its calls to."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    script = binaries / "docker"
    script.write_text(FAKE_DOCKER, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "docker.log"
    return {"bin": binaries, "log": log, "root": tmp_path, "state": tmp_path / "state"}


@pytest.fixture
def fake_cosign(fake_docker):
    """A fake ``cosign`` on PATH, next to the fake ``docker``."""
    script = fake_docker["bin"] / "cosign"
    script.write_text(FAKE_COSIGN, encoding="utf-8")
    script.chmod(0o755)
    return fake_docker["root"] / "cosign.log"


def _base_env(fake_docker, appdata, media, **overrides):
    env = dict(os.environ)
    env["PATH"] = f"{fake_docker['bin']}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(fake_docker["log"])
    env["FAKE_DOCKER_STATE"] = str(fake_docker["state"])
    env["FAKE_COSIGN_LOG"] = str(fake_docker["root"] / "cosign.log")
    env["ALC_INSTALL_NONINTERACTIVE"] = "1"
    env["ALC_INSTALL_APPDATA"] = str(appdata)
    env["ALC_INSTALL_MEDIA"] = str(media)
    env["ALC_INSTALL_PORT"] = "17540"
    env["ALC_INSTALL_PUID"] = "99"
    env["ALC_INSTALL_PGID"] = "100"
    env["ALC_INSTALL_TZ"] = "Europe/Madrid"
    env["ALC_INSTALL_USER"] = OWNER
    env["ALC_INSTALL_PASSWORD"] = PASSWORD
    env["ALC_INSTALL_UNRAID_MNT"] = "yes"
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def run_installer(fake_docker, appdata, media, script=None, **overrides):
    env = _base_env(fake_docker, appdata, media, **overrides)
    return subprocess.run(
        ["bash", str(script or INSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def docker_calls(fake_docker) -> list[str]:
    log = fake_docker["log"]
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def install(tmp_path, fake_docker):
    """One successful non-interactive installation."""
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_text("media", encoding="utf-8")
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        "appdata": appdata,
        "media": media,
        "result": result,
        "env_file": appdata / ENV_FILE_NAME,
        "manifest": appdata / MANIFEST_NAME,
        "secret_key": appdata / SECRET_KEY_NAME,
    }


# --- The scripts themselves ------------------------------------------------


def test_both_scripts_exist_and_are_executable():
    for script in (INSTALLER, UNINSTALLER):
        assert script.is_file(), f"{script.name} is missing"
        assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_both_scripts_use_bash_with_strict_mode():
    for script in (INSTALLER, UNINSTALLER):
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash"), script.name
        assert "set -euo pipefail" in text, script.name


def test_both_scripts_pass_the_bash_syntax_check():
    for script in (INSTALLER, UNINSTALLER):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_both_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    for script in (INSTALLER, UNINSTALLER):
        result = subprocess.run(
            [shellcheck, "-S", "warning", str(script)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{script.name}: {result.stdout}{result.stderr}"


# --- The installer ---------------------------------------------------------


def test_the_env_file_is_owner_only_and_holds_the_login(install):
    assert install["env_file"].is_file()
    assert mode_of(install["env_file"]) == 0o600
    content = install["env_file"].read_text(encoding="utf-8")
    assert f"ALC_AUTH_USER={OWNER}" in content
    assert f"ALC_AUTH_PASSWORD={PASSWORD}" in content
    assert "PUID=99" in content
    assert "PGID=100" in content
    assert "TZ=Europe/Madrid" in content


def test_the_password_never_appears_in_the_docker_command_line(install, fake_docker):
    calls = docker_calls(fake_docker)
    assert calls, "the installer never called docker"
    assert PASSWORD not in "\n".join(calls)
    assert "--env-file" in "\n".join(calls)


def test_the_container_is_created_with_the_s8_mounts_and_labels(install, fake_docker):
    run_calls = [call for call in docker_calls(fake_docker) if call.startswith("run ")]
    assert len(run_calls) == 1, run_calls
    call = run_calls[0]
    assert "-d" in call
    assert "--restart unless-stopped" in call
    assert "--env-file" in call
    assert "17540:17540" in call
    assert f"{install['appdata']}:/config" in call
    assert f"{install['media']}:/media:ro" in call
    assert "/mnt:/unraid/mnt:ro" in call
    assert "ghcr.io/almali-dev/almali-library-curator@sha256:" in call
    assert "--label" in call
    assert "install-unraid.sh" in call


def test_the_unraid_mount_is_optional(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_UNRAID_MNT="no")
    assert result.returncode == 0, result.stdout + result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "/unraid/mnt" not in call
    assert "ALC_UNRAID_ROOT" not in (appdata / ENV_FILE_NAME).read_text(encoding="utf-8")


def test_the_secret_key_is_created_owner_only_with_at_least_32_bytes(install):
    assert install["secret_key"].is_file()
    assert mode_of(install["secret_key"]) == 0o600
    assert install["secret_key"].stat().st_size >= 32


def test_an_existing_secret_key_is_kept(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    appdata.mkdir()
    key = appdata / SECRET_KEY_NAME
    key.write_bytes(b"k" * 48)
    key.chmod(0o600)
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    assert key.read_bytes() == b"k" * 48
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["secret_key_created"] is False


def test_puid_zero_is_refused(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_PUID="0")
    assert result.returncode != 0
    assert "PUID" in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    assert not (appdata / ENV_FILE_NAME).exists()


def test_pgid_zero_is_refused(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_PGID="0")
    assert result.returncode != 0
    assert "PGID" in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_a_missing_password_is_refused(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_PASSWORD=None)
    assert result.returncode != 0
    assert "ALC_INSTALL_PASSWORD" in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def _interactive_env(fake_docker):
    """The environment of an interactive run: no answer is preset."""
    env = dict(os.environ)
    env["PATH"] = f"{fake_docker['bin']}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(fake_docker["log"])
    env["FAKE_DOCKER_STATE"] = str(fake_docker["state"])
    env["FAKE_COSIGN_LOG"] = str(fake_docker["root"] / "cosign.log")
    for name in list(env):
        if name.startswith("ALC_INSTALL_"):
            del env[name]
    return env


def _interactive_answers(appdata, media, *, port="17540", bind="", user="owner",
                         password="a-strong-password", unraid_mnt="no"):
    """The answers of an interactive run, in the order the installer asks."""
    return "\n".join(
        [
            str(appdata),
            str(media),
            unraid_mnt,
            port,
            bind,
            "99",
            "100",
            "Europe/Madrid",
            user,
            password,
            password,
            "",
        ]
    )


def test_the_interactive_path_asks_and_never_echoes_the_password(tmp_path, fake_docker):
    """The default run of the installer: it asks, and the password is silent."""
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    env = _interactive_env(fake_docker)
    answers = _interactive_answers(appdata, media, port="17541")
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        input=answers,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a-strong-password" not in result.stdout
    assert "a-strong-password" not in result.stderr
    content = (appdata / ENV_FILE_NAME).read_text(encoding="utf-8")
    assert "ALC_AUTH_USER=owner" in content
    assert "ALC_AUTH_PASSWORD=a-strong-password" in content
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "17541:17540" in call


def test_an_existing_container_with_the_same_name_is_refused(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    fake_docker["state"].mkdir(parents=True, exist_ok=True)
    (fake_docker["state"] / APP_NAME).write_text("", encoding="utf-8")
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode != 0
    assert APP_NAME in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_the_manifest_records_what_was_created(install):
    assert install["manifest"].is_file()
    manifest = json.loads(install["manifest"].read_text(encoding="utf-8"))
    assert manifest["container_name"] == APP_NAME
    assert manifest["env_file"] == str(install["env_file"])
    assert manifest["secret_key"] == str(install["secret_key"])
    assert manifest["secret_key_created"] is True
    assert manifest["app_data"] == str(install["appdata"])
    assert manifest["image"].startswith("ghcr.io/almali-dev/almali-library-curator")
    assert manifest["start_verified"] is True


def test_the_image_tag_is_overridable(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_TAG="v0.18.0")
    assert result.returncode == 0, result.stdout + result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "pull ghcr.io/almali-dev/almali-library-curator:v0.18.0" in docker_calls(fake_docker)
    assert "ghcr.io/almali-dev/almali-library-curator@sha256:" in call


# --- The bind address (S21 rule 6) -----------------------------------------


def test_the_bind_address_is_published_when_it_is_given(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_BIND="127.0.0.1")
    assert result.returncode == 0, result.stdout + result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "-p 127.0.0.1:17540:17540" in call


def test_the_bind_address_defaults_to_the_whole_lan(tmp_path, fake_docker):
    """The LAN default stays: the port is published on every interface."""
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "-p 0.0.0.0:17540:17540" in call


def test_an_unusable_bind_address_is_refused(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_BIND="not a host")
    assert result.returncode != 0
    assert "bind address" in (result.stdout + result.stderr).lower()
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_the_interactive_prompt_offers_the_lan_default_bind_address(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    env = _interactive_env(fake_docker)
    answers = _interactive_answers(appdata, media, bind="127.0.0.1")
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        input=answers,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bind address" in result.stderr
    assert "0.0.0.0" in result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "-p 127.0.0.1:17540:17540" in call


# --- The image signature (S21 rule 7) ---------------------------------------


def test_a_valid_signature_lets_the_installation_continue(tmp_path, fake_docker, fake_cosign):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    calls = fake_cosign.read_text(encoding="utf-8")
    assert "verify" in calls
    assert "--certificate-identity-regexp" in calls
    # The identity is the publish workflow of this repository on a release tag.
    assert (
        "^https://github.com/almali-dev/almali-library-curator/"
        "\\.github/workflows/publish\\.yml@refs/tags/v"
    ) in calls
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in calls
    assert "ghcr.io/almali-dev/almali-library-curator" in calls


def test_a_failing_signature_stops_the_installation(tmp_path, fake_docker, fake_cosign):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, FAKE_COSIGN_RESULT="fail")
    assert result.returncode != 0
    assert "ghcr.io/almali-dev/almali-library-curator" in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    assert not appdata.exists()


def test_without_cosign_the_installer_warns_and_continues(tmp_path, fake_docker):
    if shutil.which("cosign") is not None:
        pytest.skip("cosign is installed on this host")
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cosign" in (result.stdout + result.stderr)
    assert [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_without_cosign_a_required_signature_stops_the_installation(tmp_path, fake_docker):
    if shutil.which("cosign") is not None:
        pytest.skip("cosign is installed on this host")
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_REQUIRE_SIGNATURE="1")
    assert result.returncode != 0
    assert "ghcr.io/almali-dev/almali-library-curator" in result.stdout + result.stderr
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    assert not appdata.exists()


# --- The start verification (S21 rule 8) -----------------------------------


def test_a_healthy_container_completes_the_installation(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, FAKE_DOCKER_HEALTH="healthy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installation complete" in result.stdout + result.stderr
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["start_verified"] is True


def test_a_container_that_becomes_healthy_after_a_few_polls_completes(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(
        fake_docker,
        appdata,
        media,
        FAKE_DOCKER_HEALTH="starting,starting,healthy",
        ALC_INSTALL_START_INTERVAL="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (appdata / MANIFEST_NAME).is_file()


def test_an_unhealthy_container_stops_the_installation(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, FAKE_DOCKER_HEALTH="unhealthy")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert APP_NAME in output
    assert "docker logs" in output
    assert "Installation complete" not in output
    # S21 rule 8 (amended, L141): the manifest exists as soon as the
    # container does, and it says the start was not verified.
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["start_verified"] is False
    assert manifest["container_name"] == APP_NAME
    assert manifest["env_file"] == str(appdata / ENV_FILE_NAME)
    assert manifest["secret_key"] == str(appdata / SECRET_KEY_NAME)


def test_an_exited_container_stops_the_installation(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, FAKE_DOCKER_HEALTH="exited")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert APP_NAME in output
    assert "docker logs" in output
    assert "Installation complete" not in output
    # S21 rule 8 (amended, L141): the manifest exists as soon as the
    # container does, and it says the start was not verified.
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["start_verified"] is False
    assert manifest["container_name"] == APP_NAME
    assert manifest["env_file"] == str(appdata / ENV_FILE_NAME)
    assert manifest["secret_key"] == str(appdata / SECRET_KEY_NAME)


def test_the_start_wait_uses_wall_clock_and_rejects_a_zero_interval(tmp_path, fake_docker):
    media = tmp_path / "media"
    media.mkdir()
    zero = run_installer(
        fake_docker, tmp_path / "zero", media,
        ALC_INSTALL_CONTAINER_NAME="zero-interval-container",
        ALC_INSTALL_START_INTERVAL="0",
        ALC_INSTALL_START_TIMEOUT="0",
    )
    started = time.monotonic()
    wedged = run_installer(
        fake_docker, tmp_path / "wedged", media,
        ALC_INSTALL_CONTAINER_NAME="wedged-inspect-container",
        FAKE_DOCKER_INSPECT_SLEEP="10",
        ALC_INSTALL_START_TIMEOUT="1",
        ALC_INSTALL_START_INTERVAL="1",
    )
    assert zero.returncode != 0
    assert "poll interval" in (zero.stdout + zero.stderr).lower()
    assert not [c for c in docker_calls(fake_docker)
                if c.startswith("run ") and "zero-interval-container" in c]
    assert wedged.returncode != 0
    assert time.monotonic() - started < 5


def test_the_pulled_digest_matches_the_requested_repository(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    wrong = f"registry.example:5000/team/other@sha256:{'b' * 64}"
    right = f"registry.example:5000/team/app@sha256:{'c' * 64}"
    result = run_installer(
        fake_docker, appdata, media,
        ALC_INSTALL_IMAGE="registry.example:5000/team/app:tag",
        FAKE_DOCKER_REPO_DIGESTS=f"{wrong}\n{right}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    run_call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert right in run_call
    assert wrong not in run_call


def test_a_container_that_never_becomes_healthy_times_out(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(
        fake_docker,
        appdata,
        media,
        FAKE_DOCKER_HEALTH="starting",
        ALC_INSTALL_START_TIMEOUT="3",
        ALC_INSTALL_START_INTERVAL="1",
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert APP_NAME in output
    assert "docker logs" in output
    assert "Installation complete" not in output
    # S21 rule 8 (amended, L141): the manifest exists as soon as the
    # container does, and it says the start was not verified.
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["start_verified"] is False
    assert manifest["container_name"] == APP_NAME
    assert manifest["env_file"] == str(appdata / ENV_FILE_NAME)
    assert manifest["secret_key"] == str(appdata / SECRET_KEY_NAME)


# --- The download checksum -------------------------------------------------


def test_a_checksum_mismatch_refuses_to_run(tmp_path, fake_docker):
    """S8: the installer verifies its own download before doing anything."""
    script = tmp_path / "install-unraid.sh"
    shutil.copy2(INSTALLER, script)
    (tmp_path / "install-unraid.sh.sha256").write_text(
        f"{'0' * 64}  install-unraid.sh\n", encoding="utf-8"
    )
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, script=script)
    assert result.returncode != 0
    assert "checksum" in (result.stdout + result.stderr).lower()
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    assert not (appdata / ENV_FILE_NAME).exists()


def test_a_matching_checksum_lets_the_installer_run(tmp_path, fake_docker):
    import hashlib

    script = tmp_path / "install-unraid.sh"
    shutil.copy2(INSTALLER, script)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    (tmp_path / "install-unraid.sh.sha256").write_text(
        f"{digest}  install-unraid.sh\n", encoding="utf-8"
    )
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, script=script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_without_a_checksum_file_the_installer_warns_and_continues(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checksum" in (result.stdout + result.stderr).lower()


# --- The uninstaller -------------------------------------------------------


def run_uninstaller(fake_docker, appdata, *args, stdin="", **env_overrides):
    env = dict(os.environ)
    env["PATH"] = f"{fake_docker['bin']}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(fake_docker["log"])
    env["FAKE_DOCKER_STATE"] = str(fake_docker["state"])
    env.pop("ALC_UNINSTALL_NONINTERACTIVE", None)
    for key, value in env_overrides.items():
        env[key] = str(value)
    return subprocess.run(
        ["bash", str(UNINSTALLER), "--app-data", str(appdata), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_uninstaller_removes_the_container_and_the_env_file_and_keeps_the_app_data(
    install, fake_docker
):
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, install["appdata"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = docker_calls(fake_docker)[before:]
    removals = [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))]
    assert removals, calls
    assert all(APP_NAME in call for call in removals), calls
    assert not install["env_file"].exists()
    assert install["appdata"].is_dir()
    assert (install["media"] / "movie.mkv").read_text(encoding="utf-8") == "media"


def test_the_uninstaller_keeps_the_secret_key_it_did_not_create(install, fake_docker):
    """A key the installer found is not the installer's to delete."""
    manifest = json.loads(install["manifest"].read_text(encoding="utf-8"))
    manifest["secret_key_created"] = False
    install["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    result = run_uninstaller(fake_docker, install["appdata"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert install["secret_key"].exists()


def test_the_uninstaller_asks_before_deleting_the_app_data(install, fake_docker):
    result = run_uninstaller(fake_docker, install["appdata"], stdin="y\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not install["appdata"].exists()
    assert (install["media"] / "movie.mkv").exists()


def test_an_unattended_uninstall_keeps_the_app_data(install, fake_docker):
    result = run_uninstaller(
        fake_docker,
        install["appdata"],
        stdin="y\n",
        ALC_UNINSTALL_NONINTERACTIVE="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert install["appdata"].is_dir()


def test_the_uninstaller_removes_exactly_the_container_the_manifest_names(install, fake_docker):
    manifest = json.loads(install["manifest"].read_text(encoding="utf-8"))
    manifest["container_name"] = "another-container"
    install["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    fake_docker["state"].mkdir(parents=True, exist_ok=True)
    (fake_docker["state"] / "another-container").write_text("", encoding="utf-8")
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, install["appdata"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = docker_calls(fake_docker)[before:]
    removals = [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))]
    assert removals, calls
    assert all("another-container" in call for call in removals), calls
    assert not any(APP_NAME in call for call in removals), calls


def test_the_uninstaller_refuses_without_a_manifest(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    result = run_uninstaller(fake_docker, appdata)
    assert result.returncode != 0
    assert not [c for c in docker_calls(fake_docker) if c.startswith(("rm ", "stop "))]


def test_the_uninstaller_never_touches_the_media(install, fake_docker):
    run_uninstaller(fake_docker, install["appdata"], stdin="y\n")
    assert (install["media"] / "movie.mkv").read_text(encoding="utf-8") == "media"


def test_the_uninstaller_removes_what_a_failed_start_left_behind(tmp_path, fake_docker):
    """S21 rule 8 (amended, L141): a failed start is not an orphan."""
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, FAKE_DOCKER_HEALTH="unhealthy")
    assert result.returncode != 0
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["start_verified"] is False
    before = len(docker_calls(fake_docker))
    uninstall = run_uninstaller(fake_docker, appdata)
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    calls = docker_calls(fake_docker)[before:]
    removals = [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))]
    assert removals, calls
    assert all(manifest["container_name"] in call for call in removals), calls
    assert not (appdata / ENV_FILE_NAME).exists()


# --- The deploy guide ------------------------------------------------------


def test_the_deploy_guide_documents_the_delivery():
    assert GUIDE.is_file(), "docs/UNRAID.md is missing"
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (
        "install-unraid.sh",
        "install-unraid.sh.sha256",
        "sha256sum",
        "uninstall-unraid.sh",
        "unraid-template.xml",
        "17540",
        "/media",
        "/unraid/mnt",
        "ALC_TRUSTED_PROXIES",
        "ALC_SECURE_COOKIES",
        "Not affiliated with or endorsed by Plex Inc.",
    ):
        assert needle in text, f"docs/UNRAID.md does not mention {needle}"


def test_the_deploy_guide_documents_the_hardening_of_the_installer():
    # S21 rules 6-8: the bind address, the signature check and the start
    # verification are documented where the owner reads about them.
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (
        "ALC_INSTALL_BIND",
        "0.0.0.0",
        "127.0.0.1",
        "cosign",
        "ALC_INSTALL_REQUIRE_SIGNATURE",
        "ALC_INSTALL_START_TIMEOUT",
        "docker logs",
    ):
        assert needle in text, f"docs/UNRAID.md does not mention {needle}"


def test_the_published_checksum_matches_the_committed_installer():
    # S8: the installer verifies its own download checksum, so the checksum
    # file published next to it must always match the committed script.
    import hashlib
    root = Path(__file__).resolve().parents[2]
    published = (root / "install-unraid.sh.sha256").read_text().split()
    assert published[1].lstrip("*") == "install-unraid.sh"
    assert published[0] == hashlib.sha256((root / "install-unraid.sh").read_bytes()).hexdigest()


# --- Review corrections (review-43c48a90e59e1d36) -------------------------

@pytest.mark.parametrize("layout", ["same", "app_data_holds_media", "media_holds_app_data"])
def test_app_data_overlapping_the_media_is_refused(tmp_path, fake_docker, layout):
    if layout == "same":
        appdata = media = tmp_path / "share"
    elif layout == "app_data_holds_media":
        appdata, media = tmp_path / "share", tmp_path / "share" / "media"
    else:
        media, appdata = tmp_path / "share", tmp_path / "share" / "appdata"
    media.mkdir(parents=True)
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode != 0
    assert "overlap" in (result.stdout + result.stderr).lower()
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]
    assert not (appdata / ENV_FILE_NAME).exists()


def test_the_uninstaller_never_deletes_app_data_that_holds_the_media(install, fake_docker):
    manifest = json.loads(install["manifest"].read_text(encoding="utf-8"))
    assert manifest["media"] == str(install["media"])
    inside = install["appdata"] / "media"
    inside.mkdir()
    (inside / "show.mkv").write_text("media", encoding="utf-8")
    manifest["media"] = str(inside)
    install["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    result = run_uninstaller(fake_docker, install["appdata"], stdin="y\n")
    assert result.returncode != 0
    assert (inside / "show.mkv").read_text(encoding="utf-8") == "media"
    assert install["appdata"].is_dir()


def test_a_rerun_over_a_running_installation_keeps_its_env_file(install, fake_docker):
    before = install["env_file"].read_bytes()
    result = run_installer(fake_docker, install["appdata"], install["media"],
                           ALC_INSTALL_PASSWORD="another-password-123")
    assert result.returncode != 0
    assert install["env_file"].read_bytes() == before


def test_an_existing_env_file_is_never_overwritten(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    appdata.mkdir()
    existing = appdata / ENV_FILE_NAME
    existing.write_text("ALC_AUTH_USER=kept\n", encoding="utf-8")
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode != 0
    assert existing.read_text(encoding="utf-8") == "ALC_AUTH_USER=kept\n"
    assert not [c for c in docker_calls(fake_docker) if c.startswith("run ")]


def test_the_guide_non_interactive_example_sets_every_required_answer():
    # A non-interactive run stops on any missing answer, so the guide's
    # example must name every one of them (verifier finding, L127).
    import re
    root = Path(__file__).resolve().parents[2]
    guide = (root / "docs" / "UNRAID.md").read_text(encoding="utf-8")
    example = next(block for block in re.findall(r"```bash\n(.*?)```", guide, re.S)
                   if "ALC_INSTALL_NONINTERACTIVE=1" in block)
    required = {"ALC_INSTALL_APPDATA", "ALC_INSTALL_MEDIA", "ALC_INSTALL_UNRAID_MNT",
                "ALC_INSTALL_PORT", "ALC_INSTALL_PUID", "ALC_INSTALL_PGID", "ALC_INSTALL_TZ",
                "ALC_INSTALL_USER", "ALC_INSTALL_PASSWORD"}
    assert required <= set(re.findall(r"\b(ALC_INSTALL_[A-Z_]+)=", example))
    assert "choose-a-strong-password" not in example  # never typed into shell history


def test_verification_and_run_use_the_pulled_digest(tmp_path, fake_docker, fake_cosign):
    media = tmp_path / "media"
    media.mkdir()
    digest = "ghcr.io/almali-dev/almali-library-curator@sha256:" + "a" * 64
    result = run_installer(fake_docker, tmp_path / "appdata", media)
    assert result.returncode == 0, result.stdout + result.stderr
    # cosign can resolve a mutable tag to a different digest; never give it one.
    assert fake_cosign.read_text().splitlines()[0].endswith(digest)
    assert next(c for c in docker_calls(fake_docker) if c.startswith("run ")).endswith(digest)


def test_healthy_health_does_not_override_an_exited_state(tmp_path, fake_docker):
    media = tmp_path / "media"
    media.mkdir()
    appdata = tmp_path / "appdata"
    result = run_installer(fake_docker, appdata, media, FAKE_DOCKER_STATUS="exited")
    assert result.returncode == 1
    assert "Installation complete" not in result.stdout + result.stderr
    assert "exited" in result.stderr and "docker logs" in result.stderr
    assert json.loads((appdata / MANIFEST_NAME).read_text())["start_verified"] is False


@pytest.mark.parametrize("section", ["### What the installer creates", "## Update"])
def test_documented_commands_are_constructed_by_bash(section, fake_docker, tmp_path):
    guide = GUIDE.read_text()
    if section.startswith("###"):
        block = guide.split(section)[0].rsplit("```bash\n", 1)[1].split("```", 1)[0]
        prefix = 'bash() { printf "INSTALL:%s:%s:%s\\n" "$ALC_INSTALL_PORT" "$ALC_INSTALL_BIND" "$ALC_INSTALL_PUID"; };\n'
    else:
        block = guide.split(section, 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
        # Parse the published command without touching the real boot device
        # or running an upgrade against the host's default app data path.
        prefix = 'cd() { :; }; bash() { printf "UPGRADE:%s:%s:%s:%s\\n" "$@"; };\n'
    env = _base_env(fake_docker, tmp_path / "appdata", tmp_path / "media")
    result = subprocess.run(["bash", "-e", "-c", prefix + block], env=env,
                            input="example-password\n", capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    if section.startswith("###"):
        assert "INSTALL:17540:0.0.0.0:99" in result.stdout
    else:
        assert "UPGRADE:install-unraid.sh:--upgrade:--app-data:/mnt/user/appdata/almali-library-curator" in result.stdout
        assert docker_calls(fake_docker) == []
