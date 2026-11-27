"""#146: the root-run uninstaller deletes only the paths the installer created.

The manifest lives inside the app data directory, which the app container is
allowed to write, and every path it names is removed as root. The uninstaller
therefore accepts only the files the installer creates: ``env_file`` and
``secret_key`` (and the password file, for the same reason) must be the
expected names directly inside the app data directory, with no ``..`` that
climbs out and no symlink that leaves it.
"""
from pathlib import Path

import json

import pytest

from test_unraid_installer import (
    ENV_FILE_NAME, MANIFEST_NAME, PASSWORD_FILE_NAME, SECRET_KEY_NAME,
    docker_calls, fake_docker as fake_docker, mode_of,
    run_installer, run_uninstaller,
)
from test_unraid_upgrade import snapshot

APP_NAME = "almali-library-curator"


@pytest.fixture
def install(tmp_path, fake_docker):
    """One successful install, with app data the app has already written to."""
    appdata, media = tmp_path / "appdata", tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_bytes(b"media")
    result = run_installer(fake_docker, appdata, media)
    assert result.returncode == 0, result.stdout + result.stderr
    (appdata / f"{APP_NAME}.db").write_bytes(b"database")
    return appdata, media


def _write_manifest(appdata, **values):
    path = appdata / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(values)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _container_name(appdata):
    return json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))["container_name"]


def _removals(calls):
    return [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))]


@pytest.mark.parametrize("case", ["outside", "other_name", "dotdot", "symlink"])
@pytest.mark.parametrize("key", ["env_file", "secret_key"])
def test_the_uninstaller_refuses_a_manifest_path_the_installer_never_created(
        install, fake_docker, tmp_path, key, case):
    """S2: any other path deletes nothing and stops with a clear message."""
    appdata, media = install
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "planted"
    target.write_bytes(b"not ours")
    expected = appdata / (ENV_FILE_NAME if key == "env_file" else SECRET_KEY_NAME)
    if case == "outside":
        value = str(target)
    elif case == "other_name":
        # The right directory, the wrong name: not a file the installer wrote.
        value = str(appdata / "planted")
        Path(value).write_bytes(b"not ours")
    elif case == "dotdot":
        # Climbs out with ".." and back to a real file inside the app data.
        value = f"{appdata}/sub/../{expected.name}"
    else:
        # A symlink that leaves the app data directory.
        value = str(appdata / "planted-link")
        Path(value).symlink_to(target)
    _write_manifest(appdata, **{key: value})
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert value in result.stderr
    assert str(expected) in result.stderr
    assert "nothing was removed" in result.stderr.lower()
    assert "Uninstall complete" not in result.stdout + result.stderr
    # Nothing was deleted anywhere: not the offending path, not the other
    # files of the installation, and not the container.
    assert target.read_bytes() == b"not ours"
    assert _removals(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / _container_name(appdata)).exists()
    assert snapshot(appdata) == original
    for name in (ENV_FILE_NAME, SECRET_KEY_NAME, PASSWORD_FILE_NAME, MANIFEST_NAME):
        assert (appdata / name).exists(), name
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_the_uninstaller_removes_the_paths_the_installer_created(install, fake_docker):
    """The unchanged path: a manifest of this installer uninstalls as before."""
    appdata, media = install
    env_file = appdata / ENV_FILE_NAME
    password_file = appdata / PASSWORD_FILE_NAME
    secret_key = appdata / SECRET_KEY_NAME
    container = _container_name(appdata)
    assert mode_of(secret_key) == 0o600
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Uninstall complete" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert [c for c in calls if c.startswith("rm ")] == [f"rm -f {container}"]
    assert not env_file.exists()
    assert not password_file.exists()
    assert not secret_key.exists()
    # The app data the owner keeps is untouched, and the media never is.
    assert (appdata / f"{APP_NAME}.db").read_bytes() == b"database"
    assert (appdata / MANIFEST_NAME).exists()
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_the_uninstaller_keeps_the_signing_key_the_installer_did_not_create(
        install, fake_docker):
    """The refusal must not turn a key the owner brought into ours (S2)."""
    appdata, media = install
    _write_manifest(appdata, secret_key_created=False)
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (appdata / SECRET_KEY_NAME).exists()


def test_the_uninstaller_checks_every_path_before_it_removes_anything(
        install, fake_docker, tmp_path):
    """The password file is deleted as root too, so it passes the same check."""
    appdata, media = install
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "planted-password"
    target.write_bytes(b"not ours")
    _write_manifest(appdata, password_file=str(target))
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert str(target) in result.stderr
    assert "nothing was removed" in result.stderr.lower()
    assert target.read_bytes() == b"not ours"
    assert _removals(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / _container_name(appdata)).exists()
    for name in (ENV_FILE_NAME, PASSWORD_FILE_NAME, SECRET_KEY_NAME, MANIFEST_NAME):
        assert (appdata / name).exists(), name


@pytest.mark.parametrize("spelling", ["resolved", "trailing_slash"])
def test_the_uninstaller_accepts_the_same_file_named_differently(
        tmp_path, fake_docker, spelling):
    """A symlinked or trailing-slash app data directory is not a strange path."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_bytes(b"media")
    target = tmp_path / "appdata"
    target.mkdir()
    link = tmp_path / "appdata-link"
    link.symlink_to(target)
    assert run_installer(fake_docker, link, media).returncode == 0
    appdata = target if spelling == "resolved" else (tmp_path / "appdata-link")
    argument = str(appdata) if spelling == "resolved" else f"{appdata}/"
    result = run_uninstaller(fake_docker, argument, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (target / ENV_FILE_NAME).exists()
    assert not (target / SECRET_KEY_NAME).exists()
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_a_manifest_written_by_an_older_installer_still_uninstalls(
        install, fake_docker):
    """A manifest without the password file names the two paths as before."""
    appdata, media = install
    path = appdata / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("password_file")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (appdata / ENV_FILE_NAME).exists()
    assert not (appdata / SECRET_KEY_NAME).exists()
    assert (media / "movie.mkv").read_bytes() == b"media"
