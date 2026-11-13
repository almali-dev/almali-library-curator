"""Public shell interface contracts for recoverable installs and upgrades."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from test_unraid_installer import (
    ENV_FILE_NAME, GUIDE, INSTALLER, MANIFEST_NAME, UNINSTALLER,
    _base_env, docker_calls, mode_of,
    fake_cosign as fake_cosign, fake_docker as fake_docker,
    run_installer, run_uninstaller,
)


def upgrade(fake_docker, appdata, **overrides):
    env = _base_env(fake_docker, appdata, appdata.parent / "ignored", **overrides)
    # --upgrade must not ask for credentials or read fresh configuration answers.
    env.pop("ALC_INSTALL_PASSWORD", None)
    return subprocess.run(
        ["bash", str(INSTALLER), "--upgrade", "--app-data", str(appdata)],
        env=env, capture_output=True, text=True, timeout=30,
    )


def snapshot(appdata):
    return {str(p.relative_to(appdata)): p.read_bytes()
            for p in appdata.rglob("*") if p.is_file()}


@pytest.fixture
def custom_install(tmp_path, fake_docker, fake_cosign):
    appdata, media = tmp_path / "custom appdata", tmp_path / "custom media"
    media.mkdir()
    (media / "movie").write_bytes(b"media")
    result = run_installer(fake_docker, appdata, media,
                           ALC_INSTALL_CONTAINER_NAME="custom-curator",
                           ALC_INSTALL_PORT="18541", ALC_INSTALL_BIND="127.0.0.1")
    assert result.returncode == 0, result.stderr
    (appdata / "almali-library-curator.db").write_bytes(b"database before migration")
    (appdata / "almali-library-curator.db-wal").write_bytes(b"committed WAL")
    (appdata / "secrets.key").write_bytes(b"encryption key")
    (appdata / "secrets.json").write_bytes(b"encrypted credentials")
    return appdata, media


def test_failed_docker_run_can_be_uninstalled_and_reinstalled(tmp_path, fake_docker):
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    (media / "movie").write_bytes(b"media")
    failed = run_installer(fake_docker, appdata, media, FAKE_DOCKER_RUN_FAIL="1")
    assert failed.returncode != 0
    assert "container could not be created" in failed.stderr.lower()
    assert "Installation complete" not in failed.stderr
    manifest = json.loads((appdata / MANIFEST_NAME).read_text())
    assert manifest["start_verified"] is False
    assert manifest["env_file"] == str(appdata / ENV_FILE_NAME)
    assert manifest["secret_key_created"] is True
    assert run_uninstaller(fake_docker, appdata, "-y").returncode == 0
    assert not (appdata / ENV_FILE_NAME).exists()
    assert not (appdata / "secret_key").exists()
    assert (media / "movie").read_bytes() == b"media"
    assert run_installer(fake_docker, appdata, media).returncode == 0


def test_checksum_error_prints_expected_and_actual_before_refusal(tmp_path, fake_docker):
    script = tmp_path / "install-unraid.sh"
    shutil.copy2(INSTALLER, script)
    expected, actual = "0" * 64, hashlib.sha256(script.read_bytes()).hexdigest()
    script.with_suffix(".sh.sha256").write_text(f"{expected}  {script.name}\n")
    appdata = tmp_path / "appdata"
    result = run_installer(fake_docker, appdata, tmp_path, script=script)
    assert result.returncode != 0
    assert f"Expected {expected}, got {actual}" in result.stderr
    assert not appdata.exists()
    assert docker_calls(fake_docker) == []


def test_uninstaller_is_downloaded_to_persistent_storage_and_checksummed():
    guide = GUIDE.read_text()
    assert "cd /boot/config" in guide
    assert "curl -fsSLO https://raw.githubusercontent.com/almali-dev/almali-library-curator/main/uninstall-unraid.sh\n" in guide
    assert "sha256sum -c uninstall-unraid.sh.sha256" in guide
    checksum = UNINSTALLER.with_suffix(".sh.sha256")
    assert checksum.read_text().split()[0] == hashlib.sha256(UNINSTALLER.read_bytes()).hexdigest()


def test_upgrade_keeps_manifest_configuration_and_backs_up_first(custom_install, fake_docker, fake_cosign):
    appdata, media = custom_install
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_TAG="v0.33.0",
                     FAKE_DOCKER_BACKUP_DIR=appdata / "backups")
    assert result.returncode == 0, result.stderr
    calls = docker_calls(fake_docker)[before:]
    run = next(c for c in calls if c.startswith("run "))
    for fragment in ("--name custom-curator", "-p 127.0.0.1:18541:17540",
                     f"-v {appdata}:/config", f"-v {media}:/media:ro",
                     "-v /mnt:/unraid/mnt:ro", f"--env-file {appdata / ENV_FILE_NAME}",
                     "ai.almali-dev.app=almali-library-curator",
                     "ai.almali-dev.installer=install-unraid.sh", "ai.almali-dev.managed=true"):
        assert fragment in run
    assert calls.index("stop custom-curator") < calls.index("rm custom-curator") < calls.index(run)
    assert "pull ghcr.io/almali-dev/almali-library-curator:v0.33.0" in calls
    verifications = fake_cosign.read_text().splitlines()
    assert len(verifications) == 2
    assert verifications[-1].endswith(run.split()[-1])
    assert "--certificate-identity-regexp" in verifications[-1]
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in verifications[-1]
    backups = list((appdata / "backups").glob("pre-upgrade-*.tar"))
    assert len(backups) == 1
    assert mode_of(backups[0]) == 0o600
    with tarfile.open(backups[0]) as archive:
        for name, content in original.items():
            assert archive.extractfile(f"./{name}").read() == content
    assert "backup" in result.stderr.lower()
    for name in (ENV_FILE_NAME, "secret_key", "almali-library-curator.db"):
        assert (appdata / name).read_bytes() == original[name]
    manifest = json.loads((appdata / MANIFEST_NAME).read_text())
    assert manifest["start_verified"] is True
    assert manifest["secret_key_created"] is True
    assert manifest["image"].endswith(":v0.33.0")


@pytest.mark.parametrize("reason", ["signature", "missing_cosign", "missing_manifest", "invalid_manifest", "backup", "unmanaged"])
def test_upgrade_refusals_keep_data_and_container(custom_install, fake_docker, fake_cosign, reason):
    appdata, media = custom_install
    overrides = {}
    if reason == "signature":
        overrides["FAKE_COSIGN_RESULT"] = "fail"
        overrides["ALC_INSTALL_REQUIRE_SIGNATURE"] = "0"
    elif reason == "missing_cosign":
        # Hide only our fake verifier, without relying on host PATH contents.
        (fake_docker["bin"] / "cosign").rename(fake_docker["bin"] / "hidden-cosign")
        if shutil.which("cosign"):
            pytest.skip("host cosign is installed")
    elif reason == "missing_manifest":
        (appdata / MANIFEST_NAME).rename(appdata / "manifest-saved")
    elif reason == "invalid_manifest":
        (appdata / MANIFEST_NAME).write_text('{"container_name":"unrelated"}')
    elif reason == "unmanaged":
        overrides["FAKE_DOCKER_MANAGED"] = "false"
    else:
        (appdata / "backups").write_text("not a directory")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, **overrides)
    assert result.returncode != 0
    assert {"signature": "signature", "missing_cosign": "cosign", "missing_manifest": "manifest",
            "invalid_manifest": "manifest", "backup": "backup", "unmanaged": "not installer-managed"}[reason] in result.stderr.lower()
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("rm ", "run ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()
    if "stop custom-curator" in calls:
        assert "start custom-curator" in calls
    assert (media / "movie").read_bytes() == b"media"


def test_uninstaller_checksum_refuses_before_any_change(custom_install, fake_docker):
    appdata, media = custom_install
    script = fake_docker["root"] / "uninstall-unraid.sh"
    shutil.copy2(UNINSTALLER, script)
    script.with_suffix(".sh.sha256").write_text(f"{'0' * 64}  {script.name}\n")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = subprocess.run(["bash", str(script), "--app-data", str(appdata), "-y"],
                            env=_base_env(fake_docker, appdata, media),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr.lower()
    assert snapshot(appdata) == original
    assert docker_calls(fake_docker)[before:] == []


@pytest.mark.parametrize("mount", ["yes", "no"])
def test_legacy_manifest_retains_optional_mount_and_preexisting_key(tmp_path, fake_docker, fake_cosign, mount):
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    appdata.mkdir()
    media.mkdir()
    key = appdata / "secret_key"
    key.write_bytes(b"k" * 48)
    key.chmod(0o600)
    assert run_installer(fake_docker, appdata, media, ALC_INSTALL_UNRAID_MNT=mount,
                         ALC_INSTALL_BIND="::1").returncode == 0
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    assert manifest["secret_key_created"] is False
    manifest.pop("unraid_mnt")  # the manifest format released before this fix
    manifest_path.write_text(json.dumps(manifest))
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert ("/mnt:/unraid/mnt:ro" in run) is (mount == "yes")
    assert "-p [::1]:17540:17540" in run
    assert json.loads(manifest_path.read_text())["secret_key_created"] is False
    assert run_uninstaller(fake_docker, appdata, "-y").returncode == 0
    assert key.read_bytes() == b"k" * 48


@pytest.mark.parametrize("running", ["true", "false"])
@pytest.mark.parametrize("operation", ["tar", "mv"])
def test_archive_failure_does_not_replace_container_and_restarts_only_if_running(custom_install, fake_docker, running, operation):
    appdata, media = custom_install
    executable = fake_docker["bin"] / operation
    executable.write_text('#!/bin/sh\necho "archive failed" >&2\nexit 1\n')
    executable.chmod(0o755)
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, FAKE_DOCKER_RUNNING=running)
    assert result.returncode != 0
    assert "backup failed" in result.stderr.lower()
    calls = docker_calls(fake_docker)[before:]
    assert "stop custom-curator" in calls
    assert ("start custom-curator" in calls) is (running == "true")
    assert not any(c.startswith(("rm ", "run ")) for c in calls)
    assert {n: p for n, p in snapshot(appdata).items() if not n.startswith("backups/")} == original
    assert not list((appdata / "backups").glob("*.tar"))


def test_failed_upgrade_run_keeps_recovery_manifest_and_can_retry(custom_install, fake_docker):
    appdata, media = custom_install
    original = snapshot(appdata)
    failed = upgrade(fake_docker, appdata, FAKE_DOCKER_RUN_FAIL="1")
    assert failed.returncode != 0
    assert "container could not be created" in failed.stderr.lower()
    assert json.loads((appdata / MANIFEST_NAME).read_text())["start_verified"] is False
    for name in (ENV_FILE_NAME, "secret_key", "almali-library-curator.db"):
        assert (appdata / name).read_bytes() == original[name]
    assert upgrade(fake_docker, appdata).returncode == 0
    assert json.loads((appdata / MANIFEST_NAME).read_text())["start_verified"] is True
    assert len(list((appdata / "backups").glob("*.tar"))) == 2


def test_upgrade_without_image_override_repulls_the_manifest_image(tmp_path, fake_docker, fake_cosign):
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    image = "registry.example:5000/team/curator:stable"
    digest = "registry.example:5000/team/curator@sha256:" + "b" * 64
    assert run_installer(fake_docker, appdata, media, ALC_INSTALL_IMAGE=image,
                         FAKE_DOCKER_REPO_DIGESTS=digest).returncode == 0
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, FAKE_DOCKER_REPO_DIGESTS=digest)
    assert result.returncode == 0, result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert f"pull {image}" in calls
    assert next(c for c in calls if c.startswith("run ")).endswith(digest)
    assert json.loads((appdata / MANIFEST_NAME).read_text())["image"] == image


def test_upgrade_help_and_backup_restore_documentation():
    result = subprocess.run(["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert "--upgrade" in result.stdout and "--app-data" in result.stdout
    guide = GUIDE.read_text()
    assert "bash install-unraid.sh --upgrade --app-data" in guide
    assert "## Backup and restore" in guide
    assert "downgrade" in guide.lower()
    assert "docker rm -f almali-library-curator" not in guide
    # The guide must describe what the automatic archive holds, what a
    # failed upgrade leaves behind and what the recovery may remove.
    assert "ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL" in guide
    assert "upgrade_state" in guide
    assert "another installation" in guide


# --- The upgrade keeps the install recoverable --------------------------------


@pytest.mark.parametrize("external_kind", ["symlink", "database_path"])
def test_the_upgrade_refuses_when_the_app_data_holds_files_outside_it(
        custom_install, fake_docker, external_kind):
    """The automatic archive covers the app data directory and nothing else."""
    appdata, media = custom_install
    external = appdata.parent / "external"
    external.mkdir()
    target = external / "curator.db"
    target.write_bytes(b"external database")
    if external_kind == "symlink":
        link = appdata / "almali-library-curator.db"
        link.unlink()
        link.symlink_to(target)
    else:
        env_file = appdata / ENV_FILE_NAME
        env_file.write_text(env_file.read_text() + f"ALC_DATABASE_PATH={target}\n")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert str(target) in result.stderr
    assert "ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL" in result.stderr
    # A refused upgrade changes nothing: no stop, no removal, no archive.
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert not (appdata / "backups").exists()
    assert (fake_docker["state"] / "custom-curator").exists()
    # The owner may take the responsibility and continue after a backup.
    allowed = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL="1")
    assert allowed.returncode == 0, allowed.stderr
    assert str(target) in allowed.stderr


def test_the_upgrade_records_the_replacement_before_the_old_container_is_removed(
        custom_install, fake_docker):
    """The manifest is durable before the old container goes away."""
    appdata, media = custom_install
    watch = appdata / MANIFEST_NAME
    result = upgrade(fake_docker, appdata, FAKE_DOCKER_MANIFEST_WATCH=watch)
    assert result.returncode == 0, result.stderr
    at_removal = json.loads(Path(f"{watch}.at-rm").read_text())
    assert at_removal["container_name"] == "custom-curator"
    assert at_removal["upgrade_state"] == "upgrading"
    assert at_removal["start_verified"] is False
    final = json.loads((appdata / MANIFEST_NAME).read_text())
    assert final["start_verified"] is True
    assert final["upgrade_state"] == "complete"


def test_a_failed_container_removal_restarts_the_old_container(custom_install, fake_docker):
    """A refused removal leaves the installation running, not stopped."""
    appdata, media = custom_install
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, FAKE_DOCKER_RM_FAIL="1",
                     FAKE_DOCKER_BACKUP_DIR=appdata / "backups")
    assert result.returncode != 0
    assert "Could not remove the old container custom-curator" in result.stderr
    assert "started again" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert "stop custom-curator" in calls
    assert "start custom-curator" in calls
    assert calls.count("rm custom-curator") == 1
    assert not any(c.startswith("run ") for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()
    manifest = json.loads((appdata / MANIFEST_NAME).read_text())
    assert manifest["upgrade_state"] == "rolled-back"
    assert manifest["start_verified"] is True
    assert len(list((appdata / "backups").glob("pre-upgrade-*.tar"))) == 1


def test_a_failed_install_never_claims_another_installations_container(
        tmp_path, fake_docker, fake_cosign):
    """The recovery procedure must not be able to remove a foreign container."""
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    result = run_installer(fake_docker, appdata, media,
                           ALC_INSTALL_CONTAINER_NAME="shared-name",
                           FAKE_DOCKER_RACE_NAME="shared-name")
    assert result.returncode != 0
    assert "belongs to another installation" in result.stderr
    # The failed run left nothing that could remove the other container.
    assert not (appdata / MANIFEST_NAME).exists()
    assert not (appdata / ENV_FILE_NAME).exists()
    assert not (appdata / "secret_key").exists()
    assert (fake_docker["state"] / "shared-name").exists()


def test_the_upgrade_never_replaces_a_container_of_another_installation(
        custom_install, fake_docker):
    """Only the container this installation created may be replaced."""
    appdata, media = custom_install
    labels = fake_docker["state"] / "custom-curator.labels"
    labels.write_text("ai.almali-dev.managed=true\n"
                      "ai.almali-dev.install-attempt=another-installation\n")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "not created by this installation" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()


def test_a_failed_backup_leaves_no_archive_behind(custom_install, fake_docker):
    """A half written archive is removed: it cannot be restored."""
    appdata, media = custom_install
    archive = fake_docker["bin"] / "tar"
    archive.write_text('#!/bin/sh\necho "archive failed" >&2\nexit 1\n')
    archive.chmod(0o755)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "backup failed" in result.stderr.lower()
    assert "no archive was left behind" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert "stop custom-curator" in calls
    assert "start custom-curator" in calls
    assert not list((appdata / "backups").glob("*"))
