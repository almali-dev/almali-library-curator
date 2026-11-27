"""Static checks of the container delivery (T12, S8): Dockerfile, entrypoint, compose.

These read the files as text, so they run everywhere without Docker.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
HEALTHCHECK = ROOT / "docker" / "healthcheck.py"
COMPOSE = ROOT / "docker-compose.example.yml"
README = ROOT / "README.md"


def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile instructions as (KEYWORD, arguments), continuation lines joined."""
    joined = re.sub(r"\\\n", " ", text)
    result = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        result.append((keyword.upper(), rest.strip()))
    return result


# --- Dockerfile -----------------------------------------------------------


def test_base_image_is_a_multi_arch_official_python_image():
    froms = [args for key, args in _instructions(_text(DOCKERFILE)) if key == "FROM"]
    assert froms
    for args in froms:
        image = args.split()[0]
        # No --platform pin and an official image published for amd64 and arm64.
        assert not args.startswith("--platform"), args
        reference, _, digest = image.partition("@")
        assert re.fullmatch(r"python:3\.1[1-9]-slim(-[a-z]+)?", reference), image
        # #71: the tag alone moves; the digest is what the build really uses.
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), image


def test_the_base_image_is_pinned_by_digest_in_every_stage():
    """#71: a moving tag is replaced by the immutable manifest digest.

    Dependabot (docker ecosystem) keeps the digest current; the tag stays in
    the reference so a reader can still see which release line it is.
    """
    digests = []
    for args in [args for key, args in _instructions(_text(DOCKERFILE)) if key == "FROM"]:
        image = args.split()[0]
        assert "@sha256:" in image, args
        digests.append(image.split("@", 1)[1])
    assert digests and all(digest.startswith("sha256:") for digest in digests)


def _env() -> str:
    return " ".join(args for key, args in _instructions(_text(DOCKERFILE)) if key == "ENV")


def test_no_architecture_specific_downloads():
    text = " ".join(
        f"{key} {args}" for key, args in _instructions(_text(DOCKERFILE))
    ).lower()
    for marker in ("amd64", "x86_64", "arm64", "aarch64", "curl ", "wget ", "add http"):
        assert marker not in text, marker


def test_app_is_installed_from_the_repository_pyproject():
    instructions = _instructions(_text(DOCKERFILE))
    copies = " ".join(args for key, args in instructions if key == "COPY")
    runs = " ".join(args for key, args in instructions if key == "RUN")
    assert "pyproject.toml" in copies
    assert "almali_library_curator" in copies
    assert re.search(r"pip (wheel|install)[^&]*\s\.(\s|$)", runs) or "pip wheel" in runs
    # No dependency list duplicated outside pyproject.toml.
    assert "flask" not in runs.lower()


def test_image_resolves_the_s11_major_versions():
    """S11: Gunicorn 25 and the rest of the PSC stack, same major version.

    The caps live in pyproject.toml since T8 (S19), so the package metadata
    and the image declare the same bounds; the image installs the hashed lock.
    """
    declared = tomllib.loads(_text(ROOT / "pyproject.toml"))
    pins = {}
    for requirement in declared["project"]["dependencies"]:
        name, spec = re.match(r"([A-Za-z0-9_.-]+)(.*)", requirement).groups()
        pins[name.lower()] = spec.replace(" ", "")
    assert pins["gunicorn"] == ">=25,<28"
    assert pins["flask"] == ">=3.1,<4"
    assert pins["flask-wtf"] == ">=1.2,<2"
    assert pins["requests"] == ">=2.33,<3"
    assert pins["pydantic"] == ">=2.12,<3"
    assert pins["psutil"] == ">=7.2,<8"
    assert pins["cryptography"] == ">=46,<51"
    runs = [args for key, args in _instructions(_text(DOCKERFILE)) if key == "RUN"]
    pip_runs = [args for args in runs if "pip " in args]
    assert pip_runs, pip_runs
    # Every dependency the image installs comes from the hashed lock (T8, S19).
    assert all("--require-hashes" in args or "--no-deps" in args for args in pip_runs), pip_runs


def test_config_dir_uses_the_existing_alc_config_dir_setting():
    text = _text(DOCKERFILE)
    assert re.search(r"\bALC_CONFIG_DIR=/config\b", _env())
    assert re.search(r'^VOLUME\s+\[?"?/config"?\]?', text, re.M)


def test_gunicorn_serves_the_app_factory_on_port_17540():
    instructions = _instructions(_text(DOCKERFILE))
    cmd = " ".join(args for key, args in instructions if key == "CMD")
    assert "gunicorn" in cmd
    assert "almali_library_curator.app:create_app()" in cmd
    assert "0.0.0.0:17540" in cmd
    # One worker: the scan scheduler and the lifecycle writes stay in one process.
    assert re.search(r'"--workers",\s*"1"', cmd), cmd
    assert ("EXPOSE", "17540") in instructions


def test_puid_pgid_default_to_unraid_99_100():
    assert re.search(r"\bPUID=99\b", _env())
    assert re.search(r"\bPGID=100\b", _env())


def test_entrypoint_is_the_privilege_dropping_script():
    instructions = _instructions(_text(DOCKERFILE))
    entry = [args for key, args in instructions if key == "ENTRYPOINT"]
    assert entry and "/usr/local/bin/entrypoint.sh" in entry[-1]
    copies = " ".join(args for key, args in instructions if key == "COPY")
    assert "docker/entrypoint.sh" in copies
    # The image never pins root as the final runtime user of the app process;
    # the entrypoint starts as root only to fix /config and drop privileges.
    users = [args for key, args in instructions if key == "USER"]
    assert all(u not in {"0", "root"} for u in users[1:])


def test_healthcheck_probes_healthz():
    instructions = _instructions(_text(DOCKERFILE))
    checks = [args for key, args in instructions if key == "HEALTHCHECK"]
    assert len(checks) == 1
    assert "healthcheck.py" in checks[0]
    probe = _text(HEALTHCHECK)
    assert "/healthz" in probe
    assert "17540" in probe


# --- Unraid Docker tab labels (#123) --------------------------------------

RAW_BASE = "https://raw.githubusercontent.com/almali-dev/almali-library-curator/main"
TEMPLATE = ROOT / "unraid-template.xml"


def _final_stage_labels() -> dict[str, str]:
    """The LABEL key/value pairs of the last stage, continuations joined."""
    joined = re.sub(r"\\\n", " ", _text(DOCKERFILE))
    labels: dict[str, str] = {}
    started = False
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            started = True
            labels = {}
            continue
        if started and keyword == "LABEL":
            for match in re.finditer(r'(\S+?)=("([^"]*)"|\S+)', rest):
                labels[match.group(1)] = match.group(3) if match.group(3) is not None else match.group(2)
    return labels


def test_the_image_carries_the_unraid_docker_tab_labels():
    """#123: the label gives the icon to containers created without the template.

    install-unraid.sh creates the container with `docker run` and passes only
    its own bookkeeping labels, so the image labels are inherited there too.
    """
    labels = _final_stage_labels()
    assert labels.get("net.unraid.docker.webui") == "http://[IP]:[PORT:17540]/"
    assert labels.get("net.unraid.docker.icon"), "the final stage sets no icon label"


def test_the_icon_label_names_the_same_committed_png_as_the_template():
    icon = _final_stage_labels()["net.unraid.docker.icon"]
    assert icon.startswith(RAW_BASE + "/"), icon
    assert icon.endswith(".png"), icon
    template_icon = ET.fromstring(_text(TEMPLATE)).findtext("Icon").strip()
    assert icon == template_icon, (icon, template_icon)
    assert (ROOT / icon[len(RAW_BASE) + 1 :]).is_file(), icon


# --- Healthcheck probe ----------------------------------------------------


def _load_probe():
    namespace: dict = {"__name__": "healthcheck_under_test"}
    exec(compile(_text(HEALTHCHECK), str(HEALTHCHECK), "exec"), namespace)
    return namespace["is_healthy"]


@pytest.mark.parametrize(
    "code, body, healthy",
    [
        # L98 (d): alive and not ready without Plex is still healthy.
        (200, b'{"status": "ok", "ready": false, "missing": ["plex"]}', True),
        (200, b'{"status": "ok", "ready": true, "missing": []}', True),
        (503, b'{"status": "degraded", "ready": false, "missing": []}', False),
        (200, b'{"status": "degraded"}', False),
        (200, b"not json", False),
        (500, b"", False),
    ],
)
def test_probe_follows_the_liveness_contract(code, body, healthy):
    assert _load_probe()(code, body) is healthy


# --- Entrypoint -----------------------------------------------------------


def test_entrypoint_defaults_and_drops_privileges():
    text = _text(ENTRYPOINT)
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text
    assert re.search(r"PUID=\"?\$\{PUID:-99\}", text)
    assert re.search(r"PGID=\"?\$\{PGID:-100\}", text)
    assert "chown" in text and "/config" in text
    assert re.search(r"exec setpriv --reuid=\"?\$PUID\"? --regid=\"?\$PGID\"? --clear-groups", text)


def test_entrypoint_refuses_root_and_non_numeric_ids():
    text = _text(ENTRYPOINT)
    assert re.search(r"must be a non-root numeric id|non-root", text)
    assert "*[!0-9]*" in text


def test_entrypoint_is_executable():
    assert ENTRYPOINT.stat().st_mode & 0o111


# --- Entrypoint: the manifest is never handed to PUID:PGID (#146) ----------
#
# The entrypoint starts as root on the owner's host and gives /config to
# PUID:PGID. install-manifest.json names the host paths a root-run upgrade
# binds and a root-run uninstaller deletes, so a container that could rewrite
# it would choose what the host mounts and removes. These tests run the real
# script against a temporary /config with a faked id/chown/setpriv, so the
# exact chown the host would see is asserted without touching this machine.


def _entrypoint_run(tmp_path, files=None, directories=(), puid=1234, pgid=1235,
                    config_suffix=""):
    """Run docker/entrypoint.sh against a temporary /config.

    ``id`` answers 0 (the script only acts as root), ``chown`` records every
    path it is given instead of writing one, and ``setpriv`` returns at once.
    Returns the result, the config directory and a mapping of the path each
    chown would have touched to its owner.
    """
    bin_dir = tmp_path / "entrypoint-bin"
    bin_dir.mkdir()
    chown_log = tmp_path / "chown.log"
    (bin_dir / "id").write_text('#!/bin/sh\nprintf "0\\n"\n', encoding="utf-8")
    (bin_dir / "chown").write_text(
        "#!/bin/sh\n"
        'flags=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    case "$1" in\n'
        '        -*) flags="${flags}$1 "; shift ;;\n'
        "        *) break ;;\n"
        "    esac\n"
        "done\n"
        'owner="$1"; shift\n'
        'for path in "$@"; do\n'
        f'    printf \'%s\\t%s\\t%s\\n\' "${{flags% }}" "$owner" "$path" >> "{chown_log}"\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "setpriv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    config = tmp_path / "config"
    config.mkdir()
    for name, mode in (files or {}).items():
        path = config / name
        path.write_text("x", encoding="utf-8")
        path.chmod(mode)
    for name in directories:
        (config / name).mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PUID"] = str(puid)
    env["PGID"] = str(pgid)
    env["ALC_CONFIG_DIR"] = f"{config}{config_suffix}"
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "true"], env=env, capture_output=True, text=True, timeout=60
    )
    owners = {}
    for line in chown_log.read_text(encoding="utf-8").splitlines() if chown_log.exists() else []:
        flags, owner, path = line.split("\t")
        assert flags == "-h", f"a symlink must not be followed: {line}"
        assert path not in owners, f"{path} would be chowned twice"
        owners[path] = owner
    return result, config, owners


@pytest.mark.parametrize("config_suffix", ["", "/"])
def test_the_entrypoint_never_hands_the_manifest_to_the_container_user(tmp_path, config_suffix):
    """S1 (#146): the manifest keeps its root owner and its mode."""
    result, config, owners = _entrypoint_run(
        tmp_path,
        {"install-manifest.json": 0o600, "settings.json": 0o640,
         "almali-library-curator.db": 0o644},
        directories=("posters",),
        config_suffix=config_suffix,
    )
    assert result.returncode == 0, result.stderr
    manifest = str(config / "install-manifest.json")
    # The manifest stays root-owned: the container can neither rewrite it nor
    # hand it to anyone else.
    assert owners[manifest] == "0:0"
    assert mode_of(config / "install-manifest.json") == 0o600
    # Every other entry is still handed to PUID:PGID, exactly as today.
    for name in ("settings.json", "almali-library-curator.db", "posters", ""):
        assert owners[str(config / name) if name else str(config)] == "1234:1235"


def test_the_entrypoint_hands_every_file_over_without_a_manifest(tmp_path):
    """The unchanged path: an image that has no manifest behaves as before."""
    result, config, owners = _entrypoint_run(
        tmp_path, {"settings.json": 0o600}, directories=("posters",)
    )
    assert result.returncode == 0, result.stderr
    assert set(owners.values()) == {"1234:1235"}, owners
    assert str(config / "install-manifest.json") not in owners


# --- .dockerignore --------------------------------------------------------


def test_dockerignore_excludes_development_and_planning_files():
    lines = {line.strip().rstrip("/") for line in _text(DOCKERIGNORE).splitlines()}
    for entry in (".git", ".venv", ".review", "odd", "design", "tests", "**/__pycache__",
                  ".pytest_cache", "docs"):
        assert entry in lines, entry


def test_dockerignore_keeps_the_build_inputs():
    lines = {line.strip() for line in _text(DOCKERIGNORE).splitlines()}
    for kept in ("pyproject.toml", "almali_library_curator", "docker", "README.md", "Dockerfile"):
        assert kept not in lines


# --- Compose example ------------------------------------------------------


def test_compose_mounts_follow_s8():
    text = _text(COMPOSE)
    active = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r"^\s*-\s*\S+:/config(:rw)?\s*$", active, re.M)
    assert re.search(r"^\s*-\s*\S+:/media:ro\s*$", active, re.M)
    # #42: the Unraid-only mount and variable are commented out, so the same
    # file works on any Docker host.
    assert "/unraid/mnt" not in active
    assert "ALC_UNRAID_ROOT" not in active
    assert re.search(r"^\s*#\s*-\s*/mnt:/unraid/mnt:ro\s*$", text, re.M)
    assert re.search(r"^\s*#\s*ALC_UNRAID_ROOT:", text, re.M)
    assert "optional" in text.lower()


def test_compose_uses_the_published_image_pinned_to_the_release():
    """#42: pull and run the published image; nothing has to be built."""
    text = _text(COMPOSE)
    match = re.search(r"^\s*image:\s*(\S+)\s*$", text, re.M)
    assert match, "the compose example sets no image"
    image = match.group(1)
    assert image.startswith("ghcr.io/almali-dev/almali-library-curator:"), image
    pinned = re.search(r'^DEFAULT_IMAGE_TAG="([^"]+)"$',
                       _text(ROOT / "install-unraid.sh"), re.M).group(1)
    tag = image.split(":", 1)[1]
    assert tag == "${ALC_IMAGE_TAG:-" + pinned + "}", (tag, pinned)
    assert "latest" not in tag
    # The local build is gone from the example, not just mentioned below it.
    assert "docker build" not in text


def test_compose_port_ids_and_timezone():
    text = _text(COMPOSE)
    # S21 rule 6: the bind address is a variable and the LAN default stays.
    assert re.search(r'^\s*-\s*"\$\{ALC_INSTALL_BIND:-0\.0\.0\.0\}:17540:17540"\s*$', text, re.M)
    assert "ALC_INSTALL_BIND" in text
    assert re.search(r"^\s*PUID:\s*\"?99\"?\s*$", text, re.M)
    assert re.search(r"^\s*PGID:\s*\"?100\"?\s*$", text, re.M)
    assert re.search(r"^\s*TZ:\s*\S+", text, re.M)
    assert "privileged" not in text


def test_compose_requires_the_owner_login_without_a_default():
    # S17 rules 1-2: without a user name the app serves only the 503 page.
    text = _text(COMPOSE)
    match = re.search(r"^\s*ALC_AUTH_USER:\s*(\S.*)$", text, re.M)
    assert match, "ALC_AUTH_USER"
    assert match.group(1).strip().strip('"').startswith("${ALC_AUTH_USER:?"), match.group(1)
    assert "admin" not in text.lower()


def test_compose_accepts_any_password_source_and_requires_none():
    """#42 finding: any one of the three sources starts the app.

    A blank value is not a source: the app ignores it and fails closed with
    the 503 page when none of the three is set, so the example never demands
    the clear text it just recommended leaving out.
    """
    text = _text(COMPOSE)
    for name in ("ALC_AUTH_PASSWORD", "ALC_AUTH_PASSWORD_FILE", "ALC_AUTH_PASSWORD_HASH"):
        match = re.search(rf"^\s*{name}:\s*(\S.*)$", text, re.M)
        assert match, name
        value = match.group(1).strip().strip('"')
        assert value == "${" + name + ":-}", (name, value)
        assert ":?" not in value


def test_compose_leaves_the_dry_run_default_untouched():
    text = _text(COMPOSE)
    assert not re.search(r"^\s*ALC_DRY_RUN", text, re.M)
    assert "ALC_DRY_RUN" not in _text(DOCKERFILE)
    assert "ALC_PLEX_PATH_MAP" in text


def test_compose_runs_with_least_privilege():
    """#71: no capabilities, no privilege escalation, no writable root.

    The entrypoint starts as root only to fix /config and drop to PUID:PGID,
    so the capabilities that drop needs stay: CHOWN for the ownership fix,
    DAC_OVERRIDE so a private app data directory is still readable, and
    SETUID/SETGID for setpriv. Everything else is dropped.
    """
    text = _text(COMPOSE)
    assert re.search(r"^\s*read_only:\s*true\s*$", text, re.M)
    assert re.search(r"^\s*cap_drop:\s*\n\s*-\s*ALL\s*$", text, re.M)
    added = set(re.findall(r"^\s*-\s*([A-Z_]+)\s*$", text, re.M)) - {"ALL"}
    assert added == {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"}, added
    assert re.search(r"^\s*-\s*no-new-privileges:true\s*$", text, re.M)
    # Gunicorn's worker heartbeat needs a writable /tmp under a read-only root.
    assert re.search(r"^\s*-\s*/tmp\s*$", text, re.M)


def test_compose_offers_a_password_source_that_is_not_clear_text():
    """#71: the compose example names the file and hash alternatives."""
    text = _text(COMPOSE)
    assert "ALC_AUTH_PASSWORD_FILE" in text
    assert "ALC_AUTH_PASSWORD_HASH" in text


# --- Dependabot -----------------------------------------------------------


def test_dependabot_watches_the_docker_ecosystem():
    """#71: a pinned base image digest is only safe when a bot keeps it current."""
    text = _text(ROOT / ".github" / "dependabot.yml")
    assert re.search(r'package-ecosystem:\s*"docker"', text)
    assert re.search(r'directory:\s*"\/"', text)


# --- README ---------------------------------------------------------------


def test_readme_documents_docker():
    text = _text(README)
    section = text.split("## Docker", 1)
    assert len(section) == 2
    body = section[1].split("\n## ", 1)[0]
    for needle in ("docker build", "docker-compose.example.yml", "/config", "/media",
                   "/unraid/mnt", "PUID", "PGID", "17540", "dry-run",
                   "ALC_AUTH_USER", "ALC_AUTH_PASSWORD"):
        assert needle in body, needle


def test_readme_documents_the_password_file_and_hash_alternatives():
    """#71: the app accepts both, so the documentation must name both."""
    text = _text(README)
    assert "ALC_AUTH_PASSWORD_FILE" in text
    assert "ALC_AUTH_PASSWORD_HASH" in text


def test_the_readme_opens_the_docker_section_with_the_published_image():
    """#42: pull and run first; building the image is the second path."""
    text = _text(README)
    body = text.split("## Docker", 1)[1]
    first_block = body.split("```", 2)[1]
    pinned = re.search(r'^DEFAULT_IMAGE_TAG="([^"]+)"$',
                       _text(ROOT / "install-unraid.sh"), re.M).group(1)
    assert "ghcr.io/almali-dev/almali-library-curator" in first_block
    assert pinned in first_block
    assert "docker compose up -d" in first_block
    assert "docker build" not in first_block


def test_the_readme_explains_a_denied_pull_and_the_owner_action():
    """#42: package visibility is separate from repository visibility."""
    text = _text(README)
    lowered = text.lower()
    for needle in ("docker login ghcr.io", "read:packages", "change visibility"):
        assert needle in lowered, needle
    assert "package visibility" in lowered
    # The anonymous pull is the check that proves the package is public.
    assert "docker pull ghcr.io/almali-dev/almali-library-curator" in text
