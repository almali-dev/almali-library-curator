"""Static checks of the container delivery (T12, S8): Dockerfile, entrypoint, compose.

These read the files as text, so they run everywhere without Docker.
"""
from __future__ import annotations

from pathlib import Path
import re

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
        assert re.fullmatch(r"python:3\.1[1-9]-slim(-[a-z]+)?", image), image


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
    """S11: Gunicorn 25 and the rest of the PSC stack, same major version."""
    constraints = _text(ROOT / "docker" / "constraints.txt")
    pins = {}
    for line in constraints.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, spec = re.match(r"([A-Za-z0-9_.-]+)(.*)", line).groups()
            pins[name.lower()] = spec.replace(" ", "")
    assert pins["gunicorn"] == ">=25,<26"
    assert pins["flask"] == ">=3.1,<4"
    assert pins["flask-wtf"] == ">=1.2,<2"
    assert pins["requests"] == ">=2.33,<3"
    assert pins["pydantic"] == ">=2.12,<3"
    assert pins["pydantic-settings"] == ">=2.13,<3"
    assert pins["psutil"] == ">=7.2,<8"
    assert pins["cryptography"] == ">=46,<51"
    runs = [args for key, args in _instructions(_text(DOCKERFILE)) if key == "RUN"]
    pip_runs = [args for args in runs if "pip " in args]
    assert pip_runs and all("--constraint" in args for args in pip_runs), pip_runs


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
    assert re.search(r"^\s*-\s*\S+:/config(:rw)?\s*$", text, re.M)
    assert re.search(r"^\s*-\s*\S+:/media:ro\s*$", text, re.M)
    assert re.search(r"^\s*-\s*/mnt:/unraid/mnt:ro\s*$", text, re.M)
    assert "ALC_UNRAID_ROOT" in text
    assert "optional" in text.lower()


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
    # S17 rules 1-2: without both variables the app serves only the 503 page.
    text = _text(COMPOSE)
    for name in ("ALC_AUTH_USER", "ALC_AUTH_PASSWORD"):
        match = re.search(rf"^\s*{name}:\s*(\S.*)$", text, re.M)
        assert match, name
        assert match.group(1).strip().strip('"').startswith("${" + name + ":?"), match.group(1)
    assert "admin" not in text.lower()


def test_compose_leaves_the_dry_run_default_untouched():
    text = _text(COMPOSE)
    assert not re.search(r"^\s*ALC_DRY_RUN", text, re.M)
    assert "ALC_DRY_RUN" not in _text(DOCKERFILE)
    assert "ALC_PLEX_PATH_MAP" in text


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
