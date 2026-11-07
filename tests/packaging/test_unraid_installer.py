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
case "$1" in
    info|version|pull|ps) exit 0 ;;
    run)
        echo "f0f0f0f0f0f0f0f0f0"
        name=""
        previous=""
        for argument in "$@"; do
            if [ "${previous}" = "--name" ]; then name="${argument}"; fi
            previous="${argument}"
        done
        [ -n "${name}" ] && touch "${STATE_DIR}/${name}"
        exit 0 ;;
    rm|stop|rmi|kill)
        for argument in "$@"; do
            rm -f "${STATE_DIR}/${argument}" 2>/dev/null || true
        done
        exit 0 ;;
    inspect) exit 0 ;;
    container)
        if [ -f "${STATE_DIR}/${3:-}" ]; then exit 0; fi
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


def _base_env(fake_docker, appdata, media, **overrides):
    env = dict(os.environ)
    env["PATH"] = f"{fake_docker['bin']}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(fake_docker["log"])
    env["FAKE_DOCKER_STATE"] = str(fake_docker["state"])
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
    assert "ghcr.io/almali-dev/almali-library-curator:latest" in call
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


def test_the_interactive_path_asks_and_never_echoes_the_password(tmp_path, fake_docker):
    """The default run of the installer: it asks, and the password is silent."""
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{fake_docker['bin']}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(fake_docker["log"])
    env["FAKE_DOCKER_STATE"] = str(fake_docker["state"])
    for name in list(env):
        if name.startswith("ALC_INSTALL_"):
            del env[name]
    answers = "\n".join(
        [
            str(appdata),
            str(media),
            "no",
            "17541",
            "99",
            "100",
            "Europe/Madrid",
            "owner",
            "a-strong-password",
            "a-strong-password",
            "",
        ]
    )
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


def test_the_image_tag_is_overridable(tmp_path, fake_docker):
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_TAG="v0.18.0")
    assert result.returncode == 0, result.stdout + result.stderr
    call = [c for c in docker_calls(fake_docker) if c.startswith("run ")][0]
    assert "ghcr.io/almali-dev/almali-library-curator:v0.18.0" in call


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


def test_the_published_checksum_matches_the_committed_installer():
    # S8: the installer verifies its own download checksum, so the checksum
    # file published next to it must always match the committed script.
    import hashlib
    root = Path(__file__).resolve().parents[2]
    published = (root / "install-unraid.sh.sha256").read_text().split()
    assert published[1].lstrip("*") == "install-unraid.sh"
    assert published[0] == hashlib.sha256((root / "install-unraid.sh").read_bytes()).hexdigest()
