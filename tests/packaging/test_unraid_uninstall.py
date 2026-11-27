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
from test_unraid_upgrade import snapshot, upgrade

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


# --- Only an installer-managed container is removed (N3) ---------------------
#
# A manifest with no install_attempt comes from an older installer, so the
# attempt cannot identify the container. The installer-managed label can: a
# container this installer created always carries it.


def test_the_uninstaller_only_removes_an_installer_managed_container(
        install, fake_docker):
    """A forged manifest cannot remove a container this installer never made."""
    appdata, media = install
    path = appdata / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("install_attempt")
    manifest["container_name"] = "other-app"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    # The named container exists and carries no label at all.
    (fake_docker["state"] / "other-app").write_text("", encoding="utf-8")
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert "other-app" in result.stderr
    assert "ai.almali-dev.managed" in result.stderr
    assert "Uninstall complete" not in result.stdout + result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))], calls
    assert (fake_docker["state"] / "other-app").exists()
    for name in (ENV_FILE_NAME, PASSWORD_FILE_NAME, SECRET_KEY_NAME, MANIFEST_NAME):
        assert (appdata / name).exists(), name
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_a_legacy_manifest_still_removes_an_installer_managed_container(
        install, fake_docker):
    """The unchanged path: the label of an old installation is enough."""
    appdata, media = install
    path = appdata / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("install_attempt")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (fake_docker["state"] / f"{manifest['container_name']}.labels").write_text(
        "ai.almali-dev.managed=true\n", encoding="utf-8")
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fake_docker["state"] / manifest["container_name"]).exists()
    assert not (appdata / ENV_FILE_NAME).exists()


# --- The paths must be regular file names (N4) ------------------------------


@pytest.mark.parametrize("key,name", [
    ("env_file", ENV_FILE_NAME),
    ("secret_key", SECRET_KEY_NAME),
    ("password_file", PASSWORD_FILE_NAME),
])
def test_the_uninstaller_refuses_a_directory_looking_path(
        install, fake_docker, key, name):
    """A trailing slash is accepted by realpath but is not the file: refuse."""
    appdata, media = install
    value = f"{appdata / name}/"
    _write_manifest(appdata, **{key: value})
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert value in result.stderr
    assert str(appdata / name) in result.stderr
    assert "nothing was removed" in result.stderr.lower()
    assert "Uninstall complete" not in result.stdout + result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))], calls
    assert (fake_docker["state"] / _container_name(appdata)).exists()
    for other in (ENV_FILE_NAME, PASSWORD_FILE_NAME, SECRET_KEY_NAME, MANIFEST_NAME):
        assert (appdata / other).exists(), other


# --- The media path is validated before the overlap guard uses it (#156/S1) --
#
# The overlap guard is what keeps the app data directory away from the media
# share when the owner deletes the app data, and it reads the media path from
# the same manifest: a file inside the app data directory, which the app
# container may write. Only a value the installer can never record is refused
# up front - empty, relative, or containing ":" or ",", exactly what
# `validate_answer` of the installer rejects - because such a value means a
# forged or corrupt manifest (#146). Every other spelling the installer may
# write is accepted: the guard compares resolved paths, which is the safe
# comparison for a trailing slash, a ".." or a symlinked share (L3).

# The recorded media values the installer never writes, and the reason the run
# must give for each of them.
INVALID_MEDIA = (
    ("relative", "not an absolute path"),
    ("colon", "':' or ','"),
    ("comma", "':' or ','"),
)


def _invalid_media_value(case, tmp_path):
    if case == "relative":
        # Resolved against the caller's directory, never the recorded share.
        return "media"
    if case == "colon":
        return f"{tmp_path}/media:ro"
    return f"{tmp_path}/media,ro"


@pytest.mark.parametrize("confirm", ["requested", "kept"])
@pytest.mark.parametrize("case,reason", INVALID_MEDIA)
def test_the_uninstaller_refuses_a_media_path_the_installer_never_records(
        install, fake_docker, tmp_path, case, reason, confirm):
    """S1: a manifest no installer run could write is refused up front.

    ``requested`` answers the app data question with ``y``, ``kept`` runs
    unattended (the app data is kept): the refusal is known up front, so no
    removal happens in either case.
    """
    appdata, media = install
    value = _invalid_media_value(case, tmp_path)
    _write_manifest(appdata, media=value)
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    if confirm == "requested":
        result = run_uninstaller(fake_docker, appdata, stdin="y\n")
    else:
        result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert value in result.stderr
    assert reason in result.stderr
    assert "nothing was removed" in result.stderr.lower()
    assert "Uninstall complete" not in result.stdout + result.stderr
    # #146: the refusal is known up front, so no removal happens first.
    assert _removals(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / _container_name(appdata)).exists()
    assert snapshot(appdata) == original
    for name in (ENV_FILE_NAME, PASSWORD_FILE_NAME, SECRET_KEY_NAME, MANIFEST_NAME):
        assert (appdata / name).exists(), name
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_the_uninstaller_refuses_a_manifest_with_no_media_share(install, fake_docker):
    """S1: an empty media value names the share the guard compares with: refuse."""
    appdata, media = install
    _write_manifest(appdata, media="")
    original = snapshot(appdata)
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, stdin="y\n")
    assert result.returncode != 0
    assert "no media share" in result.stderr.lower()
    assert "nothing was removed" in result.stderr.lower()
    assert "Uninstall complete" not in result.stdout + result.stderr
    assert _removals(docker_calls(fake_docker)[before:]) == []
    assert (fake_docker["state"] / _container_name(appdata)).exists()
    assert snapshot(appdata) == original
    assert (media / "movie.mkv").read_bytes() == b"media"


# The spellings the installer really records, each written by the real
# installer through the fakes, so the uninstaller is tested against a manifest
# it can never legitimately refuse (L2, L3).
INSTALLER_SPELLINGS = ("trailing_slash", "dotdot", "symlink", "upgrade_override")


def _install_with_spelling(fake_docker, tmp_path, case):
    """One installation whose manifest the installer wrote for CASE.

    Returns the app data directory, the media share and the media value the
    installer recorded.
    """
    appdata = tmp_path / "appdata"
    media = tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_bytes(b"media")
    answer = str(media)
    if case == "dotdot":
        # A ".." that lands on the real share: validate_answer accepts it, and
        # the installer only tests that the answer is a directory, so the app
        # data must already exist for "../media" to resolve from it.
        appdata.mkdir()
        answer = f"{appdata}/../media"
    elif case == "trailing_slash":
        # validate_answer accepts a trailing slash and the answer is recorded
        # verbatim.
        answer = f"{media}/"
    elif case == "symlink":
        # A share named through a link: validate_answer accepts it.
        link = tmp_path / "media-link"
        link.symlink_to(media)
        answer = str(link)
    result = run_installer(fake_docker, appdata, media, ALC_INSTALL_MEDIA=answer)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))["media"]
    assert recorded == answer
    if case == "upgrade_override":
        # An upgrade binds the share the owner names with
        # ALC_INSTALL_UPGRADE_MEDIA, and records it verbatim too.
        answer = f"{media}/"
        result = upgrade(fake_docker, appdata, ALC_INSTALL_UPGRADE_MEDIA=answer)
        assert result.returncode == 0, result.stdout + result.stderr
        recorded = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))["media"]
        assert recorded == answer
    return appdata, media, recorded


@pytest.mark.parametrize("confirm", ["requested", "kept"])
@pytest.mark.parametrize("case", INSTALLER_SPELLINGS)
def test_a_manifest_the_installer_writes_still_uninstalls(
        tmp_path, fake_docker, case, confirm):
    """S1/L2: a manifest the installer itself writes is never refused."""
    appdata, media, recorded = _install_with_spelling(fake_docker, tmp_path, case)
    container = _container_name(appdata)
    before = len(docker_calls(fake_docker))
    if confirm == "requested":
        result = run_uninstaller(fake_docker, appdata, stdin="y\n")
    else:
        result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    # The manifest was read and accepted: the run reports the recorded value.
    assert f"Media share: {recorded}" in result.stderr
    assert "Uninstall complete" in result.stderr
    assert [c for c in docker_calls(fake_docker)[before:] if c.startswith("rm ")] == [
        f"rm -f {container}"]
    assert not (fake_docker["state"] / container).exists()
    assert not (appdata / ENV_FILE_NAME).exists()
    if confirm == "requested":
        # The guard resolves the recorded spelling: no overlap, so the owner's
        # answer is honoured.
        assert not appdata.exists()
    else:
        assert appdata.is_dir()
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_a_recorded_media_share_still_lets_the_owner_delete_the_app_data(
        install, fake_docker):
    """The unchanged path (S1): a media path the installer records behaves as today."""
    appdata, media = install
    container = _container_name(appdata)
    manifest = json.loads((appdata / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["media"] == str(media)
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, stdin="y\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Uninstall complete" in result.stderr
    calls = docker_calls(fake_docker)[before:]
    assert [c for c in calls if c.startswith("rm ")] == [f"rm -f {container}"]
    assert not appdata.exists()
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_a_symlinked_media_share_inside_the_app_data_still_keeps_it(
        install, fake_docker, tmp_path):
    """S1: the guard keeps protecting the media, whatever the spelling."""
    appdata, media = install
    # A link the installer accepts as an answer, resolving into the app data.
    link = tmp_path / "media-inside"
    link.symlink_to(appdata)
    _write_manifest(appdata, media=str(link))
    result = run_uninstaller(fake_docker, appdata, stdin="y\n")
    assert result.returncode != 0
    # The spelling is accepted (no up-front refusal) and the guard resolves it:
    # the app data is kept. The removals before the app data question keep
    # their order, so the container is already gone when the guard refuses.
    assert "never writes" not in result.stderr
    assert "it may hold the media" in result.stderr
    assert str(link) in result.stderr
    assert appdata.is_dir()
    assert (appdata / f"{APP_NAME}.db").read_bytes() == b"database"
    assert (appdata / MANIFEST_NAME).exists()
    assert not (fake_docker["state"] / _container_name(appdata)).exists()
    assert (media / "movie.mkv").read_bytes() == b"media"


def test_the_uninstaller_never_reports_a_removal_it_did_not_do(install, fake_docker):
    """A link in the last component would be removed, not the file it names."""
    appdata, media = install
    env_file = appdata / ENV_FILE_NAME
    alias = appdata / "env-alias"
    alias.symlink_to(env_file)
    _write_manifest(appdata, env_file=str(alias))
    before = len(docker_calls(fake_docker))
    result = run_uninstaller(fake_docker, appdata, "-y")
    assert result.returncode != 0
    assert str(alias) in result.stderr and str(env_file) in result.stderr
    assert "nothing was removed" in result.stderr.lower()
    assert alias.is_symlink()
    assert env_file.exists()
    calls = docker_calls(fake_docker)[before:]
    assert not [c for c in calls if c.startswith(("rm ", "rm -f", "stop "))], calls
