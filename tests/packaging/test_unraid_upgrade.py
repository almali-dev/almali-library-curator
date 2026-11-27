"""Public shell interface contracts for recoverable installs and upgrades."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from test_unraid_installer import (
    ENV_FILE_NAME, GUIDE, INSTALLER, MANIFEST_NAME, PASSWORD, PASSWORD_FILE_NAME,
    SECRET_KEY_NAME, UNINSTALLER,
    _base_env, docker_calls, mode_of,
    fake_cosign as fake_cosign, fake_docker as fake_docker,
    run_installer, run_uninstaller,
)


def upgrade(fake_docker, appdata, *, cwd=None, **overrides):
    env = _base_env(fake_docker, appdata, appdata.parent / "ignored", **overrides)
    # --upgrade must not ask for credentials or read fresh configuration answers.
    env.pop("ALC_INSTALL_PASSWORD", None)
    return subprocess.run(
        ["bash", str(INSTALLER), "--upgrade", "--app-data", str(appdata)],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=30,
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


def test_an_upgrade_pull_failure_leaves_the_container_in_place(
        custom_install, fake_docker):
    """#42: the pull hint also covers --upgrade, which removes nothing."""
    appdata, _ = custom_install
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, FAKE_DOCKER_PULL_FAIL="1")
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "could not pull" in output
    assert "docker login ghcr.io" in output
    assert "public" in output
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("rm ", "run ")) for c in calls), calls


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


def test_an_upgrade_of_a_plain_password_install_keeps_its_login(tmp_path, fake_docker, fake_cosign):
    """#71: an install made before the password file keeps working when upgraded."""
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    assert run_installer(fake_docker, appdata, media).returncode == 0
    env_file = appdata / ENV_FILE_NAME
    password_file = appdata / PASSWORD_FILE_NAME
    # Rewrite the environment the way the released installer wrote it.
    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines()
             if not line.startswith("ALC_AUTH_PASSWORD_FILE=")]
    lines.append(f"ALC_AUTH_PASSWORD={PASSWORD}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    password_file.unlink()
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("password_file")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    assert f"ALC_AUTH_PASSWORD={PASSWORD}" in env_file.read_text(encoding="utf-8")
    assert not password_file.exists()
    assert "password_file" not in json.loads(manifest_path.read_text(encoding="utf-8"))
    assert run_uninstaller(fake_docker, appdata, "-y").returncode == 0


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
    # The failed run removed the old container, so this retry has no mount to
    # compare the recorded media share with: the owner names it (N2, #146).
    assert upgrade(fake_docker, appdata).returncode != 0
    retried = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=str(media))
    assert retried.returncode == 0, retried.stderr
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


def test_upgrade_of_an_existing_latest_install_keeps_latest(tmp_path, fake_docker, fake_cosign):
    """#56: the pin applies to fresh installs; an existing install is not switched."""
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    assert run_installer(fake_docker, appdata, media, ALC_INSTALL_TAG="latest").returncode == 0
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert "pull ghcr.io/almali-dev/almali-library-curator:latest" in calls
    assert json.loads((appdata / MANIFEST_NAME).read_text())["image"].endswith(":latest")


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


def _refuse_manifest_writes(fake_docker, workspace):
    """Make the manifest's temporary file impossible to write.

    The installer streams the manifest into a ``mktemp`` file inside the app
    data directory. This shim answers that ``mktemp`` call with a symlink to a
    directory, so the write into the temporary file fails while everything
    else (the backup's own ``mktemp``) keeps working. The redirect fails the
    way a full app data share does; the move over the manifest, which is what
    must never happen, still succeeds.
    """
    broken = workspace / "broken-app-data-share"
    broken.mkdir()
    link = workspace / "manifest.temporary"
    real_mktemp = shutil.which("mktemp")
    shim = fake_docker["bin"] / "mktemp"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        "    *.manifest.*)\n"
        f'        ln -sfn "{broken}" "{link}"\n'
        f'        printf \'%s\\n\' "{link}"\n'
        "        exit 0 ;;\n"
        "esac\n"
        f'exec "{real_mktemp}" "$@"\n'
    )
    shim.chmod(0o755)
    return link


def test_a_failed_manifest_write_keeps_the_previous_manifest(
        custom_install, fake_docker, tmp_path):
    """A failed write never installs a truncated manifest (S2, #86).

    The guarded call sites disable ``set -e`` inside ``write_manifest``, so
    the write itself must be checked: otherwise the move installs the broken
    temporary file over the valid manifest and the run still claims success.
    """
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    original = manifest_path.read_bytes()
    _refuse_manifest_writes(fake_docker, tmp_path)
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    # The previous manifest is still the valid manifest the next run reads.
    assert manifest_path.is_file() and not manifest_path.is_symlink()
    assert manifest_path.read_bytes() == original
    assert "was left unchanged" in result.stderr
    assert "Installation complete" not in result.stdout + result.stderr
    assert not list(appdata.glob(".manifest.*"))
    assert (appdata / ENV_FILE_NAME).exists()
    assert (appdata / "secret_key").exists()
    assert (media / "movie").read_bytes() == b"media"


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


def test_a_failed_install_never_removes_a_preexisting_signing_key(
        tmp_path, fake_docker, fake_cosign):
    """A run that loses the name race removes only what it created (S1, #85).

    docs/UNRAID.md: an existing key is kept, so a rollback must not delete the
    owner's signing key, which would sign out every session.
    """
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    appdata.mkdir()
    media.mkdir()
    key = appdata / "secret_key"
    key.write_bytes(b"k" * 48)
    key.chmod(0o600)
    result = run_installer(fake_docker, appdata, media,
                           ALC_INSTALL_CONTAINER_NAME="shared-name",
                           FAKE_DOCKER_RACE_NAME="shared-name")
    assert result.returncode != 0
    assert "belongs to another installation" in result.stderr
    # The key it found is not the key it may delete, and it created no flag.
    assert key.read_bytes() == b"k" * 48
    assert mode_of(key) == 0o600
    assert not (appdata / MANIFEST_NAME).exists()
    assert not (appdata / ENV_FILE_NAME).exists()
    assert (fake_docker["state"] / "shared-name").exists()


@pytest.mark.parametrize("created", [True, False])
def test_the_upgrade_name_race_keeps_the_manifest_and_everything_it_describes(
        custom_install, fake_docker, created):
    """An upgrade that cannot own the name keeps its recovery record (S3, S4).

    The upgrade reaches this rollback only after the old container was
    removed, so the manifest is the only record the uninstaller can use to
    verify ownership. It is kept with the environment file, the signing key
    (and its original ownership flag) and the app data.
    """
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    recorded = json.loads(manifest_path.read_text())
    if recorded["secret_key_created"] is not created:
        recorded["secret_key_created"] = created
        manifest_path.write_text(json.dumps(recorded))
    original = snapshot(appdata)
    result = upgrade(fake_docker, appdata,
                     FAKE_DOCKER_RACE_NAME="custom-curator")
    assert result.returncode != 0
    assert "belongs to another installation" in result.stderr
    # The four are kept: the manifest, the environment file, the signing key
    # and the app data.
    assert manifest_path.is_file()
    kept = json.loads(manifest_path.read_text())
    assert kept["container_name"] == "custom-curator"
    assert kept["secret_key_created"] is created
    assert kept["install_attempt"] not in ("", "another-installation")
    assert kept["start_verified"] is False
    assert kept["upgrade_state"] == "upgrading"
    for name in (ENV_FILE_NAME, "secret_key", "almali-library-curator.db"):
        assert (appdata / name).read_bytes() == original[name]
    assert (media / "movie").read_bytes() == b"media"
    # The container of the other installation is untouched.
    assert (fake_docker["state"] / "custom-curator").exists()
    # The kept manifest lets the uninstaller refuse that container...
    refused = run_uninstaller(fake_docker, appdata, "-y")
    assert refused.returncode != 0
    assert "another-installation" in refused.stderr
    assert (appdata / ENV_FILE_NAME).exists()
    # ... and finish the cleanup once the name is free again.
    (fake_docker["state"] / "custom-curator").unlink()
    (fake_docker["state"] / "custom-curator.labels").unlink()
    recovered = run_uninstaller(fake_docker, appdata, "-y")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not (appdata / ENV_FILE_NAME).exists()
    assert (appdata / "secret_key").exists() == (not created)


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


# --- The external data check lives in the container namespace ----------------


def _add_to_env_file(appdata, line):
    env_file = appdata / ENV_FILE_NAME
    env_file.write_text(env_file.read_text() + line + "\n")
    return env_file


@pytest.mark.parametrize("key", ["ALC_DATABASE_PATH", "ALC_CONFIG_DIR"])
def test_the_upgrade_refuses_a_container_path_outside_the_archived_app_data(
        custom_install, fake_docker, key):
    """The variables are read inside the container: the host path is irrelevant."""
    appdata, media = custom_install
    # A path that does not exist on the host at all: only the container
    # namespace can decide whether the archive covers it.
    value = "/var/lib/almali-not-mounted/curator.db"
    _add_to_env_file(appdata, f"{key}={value}")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert value in result.stderr
    assert "/config" in result.stderr
    assert "ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL" in result.stderr
    # A refused upgrade changes nothing: no stop, no removal, no archive.
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert not (appdata / "backups").exists()
    assert (fake_docker["state"] / "custom-curator").exists()


@pytest.mark.parametrize("key", ["ALC_DATABASE_PATH", "ALC_CONFIG_DIR"])
def test_the_upgrade_accepts_a_path_inside_the_archived_app_data(
        custom_install, fake_docker, key):
    """A path inside /config is covered by the archive, so nothing is refused."""
    appdata, media = custom_install
    _add_to_env_file(appdata, f"{key}=/config/almali-library-curator.db")
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    assert "ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL" not in result.stderr


def test_the_upgrade_refuses_a_relative_path_outside_the_archived_app_data(
        custom_install, fake_docker):
    """A relative path is resolved the way the application resolves it."""
    appdata, media = custom_install
    _add_to_env_file(appdata, "ALC_DATABASE_PATH=data/curator.db")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "data/curator.db" in result.stderr
    assert "/data/curator.db" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert not (appdata / "backups").exists()
    assert (fake_docker["state"] / "custom-curator").exists()


def test_the_upgrade_accepts_a_relative_path_inside_the_archived_app_data(
        custom_install, fake_docker):
    """A relative path that resolves inside /config is covered."""
    appdata, media = custom_install
    _add_to_env_file(appdata, "ALC_DATABASE_PATH=./config/curator.db")
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr


# --- The uninstaller is identity-safe ---------------------------------------


def test_the_uninstaller_refuses_to_remove_a_container_of_another_installation(
        custom_install, fake_docker):
    """A foreign container survives the uninstall, and the run fails."""
    appdata, media = custom_install
    labels = fake_docker["state"] / "custom-curator.labels"
    labels.write_text("ai.almali-dev.managed=true\n"
                      "ai.almali-dev.install-attempt=another-installation\n")
    refused = upgrade(fake_docker, appdata)
    assert refused.returncode != 0
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert "custom-curator" in result.stderr
    assert "another-installation" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("rm ", "rm -f", "stop ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()
    # Nothing of the installation is removed either.
    assert (appdata / ENV_FILE_NAME).exists()
    assert (appdata / MANIFEST_NAME).exists()


def _fail_docker_inspect(fake_docker, message):
    """A docker whose ``inspect`` fails with ``message`` on stderr (S5, #91)."""
    real = fake_docker["bin"] / "docker-real"
    (fake_docker["bin"] / "docker").rename(real)
    shim = fake_docker["bin"] / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "inspect" ]; then\n'
        f'    echo "{message}" >&2\n'
        "    exit 1\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)


def test_the_uninstaller_reports_the_docker_error_that_broke_the_ownership_check(
        custom_install, fake_docker):
    """A docker hiccup is not a missing ownership label (S5, #91)."""
    appdata, media = custom_install
    message = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    _fail_docker_inspect(fake_docker, message)
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    # Docker's own error is the reported cause of the refusal.
    assert message in result.stderr
    assert "no ownership label" not in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("rm ", "rm -f", "stop ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()
    assert snapshot(appdata) == original


def test_a_legacy_manifest_only_removes_an_unlabelled_container(
        custom_install, fake_docker):
    """A manifest without an attempt may not remove a labelled container."""
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("install_attempt")
    manifest_path.write_text(json.dumps(manifest))
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert "custom-curator" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("rm ", "rm -f", "stop ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()


def test_a_legacy_manifest_removes_a_container_without_an_attempt_label(
        custom_install, fake_docker):
    """The legacy case: neither the manifest nor the container carries one."""
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("install_attempt")
    manifest_path.write_text(json.dumps(manifest))
    (fake_docker["state"] / "custom-curator.labels").write_text(
        "ai.almali-dev.managed=true\n")
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert any(c.startswith(("rm ", "rm -f")) for c in calls)
    assert not (fake_docker["state"] / "custom-curator").exists()


def test_the_upgrade_refuses_a_foreign_label_when_the_manifest_has_no_attempt(
        custom_install, fake_docker):
    """The upgrade applies the same rule as the uninstaller."""
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("install_attempt")
    manifest_path.write_text(json.dumps(manifest))
    (fake_docker["state"] / "custom-curator.labels").write_text(
        "ai.almali-dev.managed=true\n"
        "ai.almali-dev.install-attempt=another-installation\n")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "another-installation" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()


def test_the_upgrade_of_a_legacy_manifest_records_the_new_attempt(
        custom_install, fake_docker):
    """A replaced container without a label gives the manifest a new attempt."""
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("install_attempt")
    manifest_path.write_text(json.dumps(manifest))
    (fake_docker["state"] / "custom-curator.labels").write_text(
        "ai.almali-dev.managed=true\n")
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    recorded = json.loads(manifest_path.read_text())["install_attempt"]
    assert recorded
    labels = (fake_docker["state"] / "custom-curator.labels").read_text()
    assert f"ai.almali-dev.install-attempt={recorded}" in labels


def test_the_upgrade_refuses_a_container_without_an_attempt_label(
        custom_install, fake_docker):
    """A manifest with an attempt never replaces an unlabelled container."""
    appdata, media = custom_install
    (fake_docker["state"] / "custom-curator.labels").write_text(
        "ai.almali-dev.managed=true\n")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "not created by this installation" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not any(c.startswith(("stop ", "rm ", "run ")) for c in calls)
    assert (fake_docker["state"] / "custom-curator").exists()


# --- The manifest host paths are validated before they are bound (S5, #83) ---
#
# The media path of an upgrade comes from the manifest, not from a prompt, so
# it must pass exactly the same check as the prompted answer: an absolute
# path without ":" or ",", the separators Docker uses to split a bind.


def _binding_calls(calls):
    """The docker calls that pull, inspect or bind anything ("info" is a probe)."""
    return [c for c in calls
            if c.startswith(("pull", "run ", "stop ", "rm ", "inspect", "container"))]


def _tamper_manifest(appdata, **values):
    path = appdata / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest.update(values)
    path.write_text(json.dumps(manifest))
    return path


@pytest.mark.parametrize("separator", [":", ","])
def test_the_upgrade_refuses_a_manifest_media_path_docker_would_split(
        custom_install, fake_docker, tmp_path, separator):
    appdata, media = custom_install
    # The tampered path exists, so only the separator rule can refuse it.
    tampered = tmp_path / f"media{separator}ro"
    tampered.mkdir()
    _tamper_manifest(appdata, media=str(tampered))
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "cannot contain" in result.stderr
    assert str(tampered) in result.stderr
    # A refused upgrade changes nothing and binds nothing.
    assert snapshot(appdata) == original
    assert _binding_calls(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / "custom-curator").exists()
    assert (media / "movie").read_bytes() == b"media"


def test_the_upgrade_refuses_a_relative_manifest_media_path(
        custom_install, fake_docker, tmp_path):
    """A relative media would be resolved against the caller's directory."""
    appdata, media = custom_install
    # The directory exists under the caller's working directory, so the
    # "-d" test passes and only the absolute-path rule can refuse it.
    (tmp_path / "relative-media").mkdir()
    _tamper_manifest(appdata, media="relative-media")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, cwd=tmp_path)
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()
    assert "relative-media" in result.stderr
    assert snapshot(appdata) == original
    assert _binding_calls(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / "custom-curator").exists()


def test_the_upgrade_refuses_a_manifest_app_data_path_holding_a_colon(
        fake_docker, tmp_path):
    """A legacy manifest may name an app data path Docker would split."""
    appdata = tmp_path / "app:data"
    appdata.mkdir()
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "cannot contain" in result.stderr
    assert str(appdata) in result.stderr
    assert _binding_calls(docker_calls(fake_docker)[before:]) == []
    assert list(appdata.iterdir()) == []


@pytest.mark.parametrize("separator", [":", ","])
def test_the_upgrade_refuses_a_manifest_app_data_target_docker_would_split(
        tmp_path, fake_docker, fake_cosign, separator):
    """A validated symlink argument may hide a recorded path Docker would split."""
    media = tmp_path / "media"
    media.mkdir()
    target = tmp_path / f"app{separator}data"
    target.mkdir()
    appdata = tmp_path / "appdata"
    appdata.symlink_to(target)
    assert run_installer(fake_docker, appdata, media).returncode == 0
    # A legacy manifest recorded the resolved target in every path it owns, so
    # the argument passes the equality check and only the separator rule can
    # refuse this upgrade.
    _tamper_manifest(appdata, app_data=str(target),
                     env_file=str(target / ENV_FILE_NAME),
                     secret_key=str(target / SECRET_KEY_NAME))
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "cannot contain" in result.stderr
    assert str(target) in result.stderr
    assert "nothing was changed" in result.stderr
    # A refused upgrade changes nothing and binds nothing.
    assert snapshot(appdata) == original
    assert _binding_calls(docker_calls(fake_docker)[before:]) == []
    assert not (target / "backups").exists()
    assert (fake_docker["state"] / "almali-library-curator").exists()


def test_the_upgrade_accepts_a_symlinked_app_data_path(
        tmp_path, fake_docker, fake_cosign):
    """The separator rule must not refuse an ordinary symlinked app data path."""
    media = tmp_path / "media"
    media.mkdir()
    target = tmp_path / "appdata"
    target.mkdir()
    appdata = tmp_path / "appdata-link"
    appdata.symlink_to(target)
    assert run_installer(fake_docker, appdata, media).returncode == 0
    before = len(docker_calls(fake_docker))
    # The app data path is itself a symlink, so the archive guard refuses the
    # upgrade until the owner takes that responsibility; the separator check
    # must still let it through.
    result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL="1")
    assert result.returncode == 0, result.stderr
    assert "cannot contain" not in result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {appdata}:/config" in run


def test_the_upgrade_still_accepts_a_media_path_with_spaces(
        tmp_path, fake_docker, fake_cosign):
    """The separator rule must not refuse the paths an owner really uses."""
    appdata, media = tmp_path / "appdata", tmp_path / "media library (4K)"
    media.mkdir()
    assert run_installer(fake_docker, appdata, media).returncode == 0
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {media}:/media:ro" in run


# --- The manifest cannot redirect what a root-run upgrade binds (#146) -------
#
# The app data directory holds the manifest and the container writes there, so
# the media path recorded in it is not authoritative: the source the container
# really has at /media is. A manifest that names another host directory is
# refused unless the owner asks for the move deliberately with
# ALC_INSTALL_UPGRADE_MEDIA.


def _container_media(fake_docker, name="custom-curator") -> str:
    """The source the fake container really has mounted at /media."""
    for line in (fake_docker["state"] / f"{name}.mounts").read_text(encoding="utf-8").splitlines():
        source, _, destination = line.partition(":")
        if destination.split(":")[0] == "/media":
            return source
    raise AssertionError(f"the container {name} has no /media mount")


def test_the_upgrade_refuses_a_manifest_media_path_the_container_does_not_mount(
        custom_install, fake_docker, tmp_path):
    """S3: the recorded path must be the media the container really mounts."""
    appdata, media = custom_install
    assert _container_media(fake_docker) == str(media)
    # The tampered share exists, so only the mount comparison can refuse it.
    elsewhere = tmp_path / "elsewhere media"
    elsewhere.mkdir()
    _tamper_manifest(appdata, media=str(elsewhere))
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert str(elsewhere) in result.stderr
    assert str(media) in result.stderr
    assert "ALC_INSTALL_UPGRADE_MEDIA" in result.stderr
    # Refused before anything is stopped, archived or bound.
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("pull", "stop ", "rm ", "run "))], calls
    assert not (appdata / "backups").exists()
    assert (fake_docker["state"] / "custom-curator").exists()
    # Neither share was written to.
    assert (media / "movie").read_bytes() == b"media"
    assert list(elsewhere.iterdir()) == []


def test_the_upgrade_keeps_the_media_path_the_container_already_mounts(
        custom_install, fake_docker):
    """The unchanged path: the recorded share upgrades exactly as today."""
    appdata, media = custom_install
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {media}:/media:ro" in run
    assert json.loads((appdata / MANIFEST_NAME).read_text())["media"] == str(media)
    assert "ALC_INSTALL_UPGRADE_MEDIA" not in result.stderr


def test_a_fresh_install_answer_does_not_move_the_media_share_of_an_upgrade(
        custom_install, fake_docker, tmp_path):
    """ALC_INSTALL_MEDIA answers the prompt; an upgrade reads the manifest."""
    appdata, media = custom_install
    moved = tmp_path / "moved media"
    moved.mkdir()
    _tamper_manifest(appdata, media=str(moved))
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_MEDIA=str(moved))
    assert result.returncode != 0
    assert str(moved) in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("pull", "stop ", "rm ", "run "))], calls
    assert (fake_docker["state"] / "custom-curator").exists()


def test_the_media_share_moves_only_when_the_owner_asks_for_it(
        custom_install, fake_docker, tmp_path):
    """S3: the documented override is the deliberate way to move the share."""
    appdata, media = custom_install
    moved = tmp_path / "moved media"
    moved.mkdir()
    (moved / "movie").write_bytes(b"moved")
    _tamper_manifest(appdata, media=str(moved))
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=str(moved))
    assert result.returncode == 0, result.stderr
    assert str(media) in result.stderr and str(moved) in result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {moved}:/media:ro" in run
    assert json.loads((appdata / MANIFEST_NAME).read_text())["media"] == str(moved)
    assert (moved / "movie").read_bytes() == b"moved"


def test_the_media_override_must_be_a_share_that_exists(
        custom_install, fake_docker, tmp_path):
    """The override passes the same check as the prompted answer."""
    appdata, media = custom_install
    missing = tmp_path / "not there"
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=str(missing))
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("stop ", "rm ", "run "))], calls
    assert (fake_docker["state"] / "custom-curator").exists()


# --- An install made before the fix is put back on a root-owned manifest ----


def _record_chown(fake_docker, exit_code=0):
    """A fake chown: it records every call and leaves ownership alone."""
    record = fake_docker["root"] / "chown.log"
    shim = fake_docker["bin"] / "chown"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> \"{record}\"\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return record


def test_the_upgrade_gives_the_manifest_back_to_root(custom_install, fake_docker):
    """S1: a manifest the old entrypoint chowned is restored by the upgrade."""
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    record = _record_chown(fake_docker)
    result = upgrade(fake_docker, appdata)
    assert result.returncode == 0, result.stderr
    # Only the manifest is chowned, and only to root; the container user keeps
    # everything else in the app data directory.
    assert record.read_text(encoding="utf-8").splitlines() == [f"-h 0:0 -- {manifest_path}"]
    assert mode_of(manifest_path) == 0o600
    assert "root" in result.stderr.lower()
    assert (media / "movie").read_bytes() == b"media"
    assert run_uninstaller(fake_docker, appdata, "-y").returncode == 0


def test_an_upgrade_that_cannot_chown_still_verifies_the_media_it_binds(
        custom_install, fake_docker, tmp_path):
    """A run that may not chown warns and keeps the security gate (#146)."""
    appdata, media = custom_install
    record = _record_chown(fake_docker, exit_code=1)
    elsewhere = tmp_path / "elsewhere media"
    elsewhere.mkdir()
    _tamper_manifest(appdata, media=str(elsewhere))
    original = snapshot(appdata)
    refused = upgrade(fake_docker, appdata)
    assert refused.returncode != 0
    assert str(elsewhere) in refused.stderr
    # A refused upgrade does not even touch the manifest's owner.
    assert not record.exists() or record.read_text(encoding="utf-8") == ""
    assert snapshot(appdata) == original
    # The legitimate upgrade continues, with the warning that names the cause.
    _tamper_manifest(appdata, media=str(media))
    allowed = upgrade(fake_docker, appdata)
    assert allowed.returncode == 0, allowed.stderr
    assert "could not be restored" in allowed.stderr
    assert str(appdata / MANIFEST_NAME) in allowed.stderr


# --- A media share that cannot be compared is refused (N2) -------------------
#
# /config belongs to PUID:PGID, so the container can replace the manifest
# whatever the owner of the file is; root ownership alone proves nothing. The
# media share is therefore only trusted when a running container shows the
# same source at /media. A container that is gone, or one that reports no
# /media mount, cannot prove anything: the owner chooses the share explicitly.


def _forget_container(fake_docker, name="custom-curator"):
    """The installation's container is gone (removed outside the installer)."""
    for suffix in ("", ".labels", ".mounts"):
        path = fake_docker["state"] / f"{name}{suffix}"
        if path.exists():
            path.unlink()


def _drop_media_mount(fake_docker, name="custom-curator"):
    """The container exists but reports no source mounted at /media."""
    path = fake_docker["state"] / f"{name}.mounts"
    kept = [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.split(":")[1].split(":")[0] != "/media"]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


@pytest.mark.parametrize("state", ["container_gone", "no_media_mount"])
def test_the_upgrade_refuses_a_media_share_it_cannot_verify(
        custom_install, fake_docker, state):
    appdata, media = custom_install
    (_forget_container if state == "container_gone" else _drop_media_mount)(fake_docker)
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    # The refusal says why, shows the recorded path and names the variable.
    assert str(media) in result.stderr
    assert "cannot be compared" in result.stderr.lower()
    assert "ALC_INSTALL_UPGRADE_MEDIA" in result.stderr
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("pull", "stop ", "rm ", "run "))], calls
    assert not (appdata / "backups").exists()
    assert (media / "movie").read_bytes() == b"media"


@pytest.mark.parametrize("state", ["container_gone", "no_media_mount"])
def test_the_media_override_is_the_way_through_without_a_comparison(
        custom_install, fake_docker, state):
    appdata, media = custom_install
    (_forget_container if state == "container_gone" else _drop_media_mount)(fake_docker)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=str(media))
    assert result.returncode == 0, result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {media}:/media:ro" in run
    assert json.loads((appdata / MANIFEST_NAME).read_text())["media"] == str(media)


def test_the_media_override_can_also_move_the_share_without_a_container(
        custom_install, fake_docker, tmp_path):
    """Without a container the owner's answer is the only source of truth."""
    appdata, media = custom_install
    _forget_container(fake_docker)
    moved = tmp_path / "moved media"
    moved.mkdir()
    (moved / "movie").write_bytes(b"moved")
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=str(moved))
    assert result.returncode == 0, result.stderr
    run = next(c for c in docker_calls(fake_docker)[before:] if c.startswith("run "))
    assert f"-v {moved}:/media:ro" in run
    assert json.loads((appdata / MANIFEST_NAME).read_text())["media"] == str(moved)
    assert (media / "movie").read_bytes() == b"media"
    assert (moved / "movie").read_bytes() == b"moved"


# --- The manifest must be a file this installer created (N5) -----------------


@pytest.mark.parametrize("target_inside", [True, False])
def test_the_upgrade_refuses_a_symlinked_manifest(
        custom_install, fake_docker, tmp_path, target_inside):
    """A manifest symlink is never something the installer created.

    The first target lives inside the app data directory, so the external-data
    guard is not the one refusing: only the link itself can be the cause. The
    second lives outside it, where the symlink refusal must still come first:
    the link must never be read, followed or replaced.
    """
    appdata, media = custom_install
    manifest_path = appdata / MANIFEST_NAME
    target = (appdata / "manifest-real.json") if target_inside else (tmp_path / "elsewhere.json")
    target.write_bytes(manifest_path.read_bytes())
    target.chmod(0o640)
    manifest_path.unlink()
    # A relative link keeps the external-data guard out of the first case: the
    # target resolves inside /config in the container namespace.
    manifest_path.symlink_to(target.name if target_inside else target)
    owner_before = target.stat().st_uid, target.stat().st_gid
    record = _record_chown(fake_docker)
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = upgrade(fake_docker, appdata)
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert str(manifest_path) in result.stderr
    # Neither the link nor its target was followed: the owner and the mode of
    # the target are exactly what they were.
    assert manifest_path.is_symlink()
    assert manifest_path.resolve() == target
    # Nothing was archived either: the refusal comes before the backup.
    assert not (appdata / "backups").exists()
    assert (target.stat().st_uid, target.stat().st_gid) == owner_before
    assert mode_of(target) == 0o640
    assert not record.exists() or record.read_text(encoding="utf-8") == ""
    assert snapshot(appdata) == original
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("pull", "stop ", "rm ", "run "))], calls
    assert not (appdata / "backups").exists()
    assert (media / "movie").read_bytes() == b"media"
