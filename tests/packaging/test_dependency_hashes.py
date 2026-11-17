"""Static contracts for the hash-pinned image dependency locks (T8, S19).

The container image installs its third-party dependencies from committed,
hash-pinned locks: pip runs with ``--require-hashes``, so the build fails when
a requirement carries no hash or when a downloaded artifact does not match the
lock. The locks cover the image's Python version for both published
architectures (linux/amd64 and linux/arm64).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "almali_library_curator"
DOCKERFILE = ROOT / "Dockerfile"
PYPROJECT = ROOT / "pyproject.toml"
RUNTIME_LOCK = ROOT / "docker" / "requirements.lock"
BUILD_LOCK = ROOT / "docker" / "build-requirements.lock"
BUILD_MANIFEST = ROOT / "docker" / "build-requirements.in"
README = ROOT / "README.md"

# The image runs python:3.12-slim; the locks are compiled for it and every
# distribution of a pinned version is hashed, so the same lock installs on
# both published architectures (manylinux2014 is the oldest glibc they need).
IMAGE_PYTHON = "3.12"
TARGET_PLATFORMS = ("manylinux2014_x86_64", "manylinux2014_aarch64")

# Dependencies that ship architecture-specific wheels: their locked hashes must
# cover more than the wheel of the machine that generated the lock.
PLATFORM_PACKAGES = ("cffi", "cryptography", "markupsafe", "psutil", "pydantic-core")

# The distribution this repository builds; the lock names it as the reason of
# every dependency the manifest declares.
DISTRIBUTION = "almali-library-curator"

# Dependencies the application never imports because something else runs them:
# gunicorn is the server process of the image (see ``CMD`` in the Dockerfile)
# and loads ``almali_library_curator.app:create_app()`` by name.
ENTRYPOINT_DEPENDENCIES = {"gunicorn"}


def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# A pinned requirement of a compiled lock, as ``name==version``.
PINNED = r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _lock_entries(path: Path) -> dict[str, dict[str, object]]:
    """Parse a hashed lock file into {name: {"version": str, "hashes": [str]}}."""
    entries: dict[str, dict[str, object]] = {}
    current = ""
    for raw in _text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        parts = current.split()
        pinned = parts[0]
        match = re.fullmatch(PINNED, pinned)
        assert match, f"{path.relative_to(ROOT)} does not pin a version: {current}"
        hashes = [part[len("--hash="):] for part in parts[1:] if part.startswith("--hash=")]
        entries[_canonical(match.group(1))] = {"version": match.group(2), "hashes": hashes}
        current = ""
    assert not current, f"{path.relative_to(ROOT)} ends with an unfinished requirement"
    return entries


def _pyproject() -> dict:
    return tomllib.loads(_text(PYPROJECT))


def _declared_dependencies() -> dict[str, str]:
    """The runtime dependencies the package declares, as {name: specifier}."""
    declared: dict[str, str] = {}
    for requirement in _pyproject()["project"]["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*(.*)", requirement)
        assert match, f"{requirement} is not a plain requirement"
        name, specifier = match.group(1), match.group(2)
        assert specifier, f"{requirement} declares no version bound"
        declared[_canonical(name)] = f"{name}{specifier}"
    return declared


def _satisfies(version: str, specifier: str) -> bool:
    """Check a pinned version against a simple floor/cap specifier."""
    for operator, wanted in re.findall(r"(>=|<=|==|>|<|!=)\s*([0-9][^\s,<]*)", specifier):
        left, right = _version_tuple(version), _version_tuple(wanted)
        if operator == ">=" and not left >= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and not left == right:
            return False
        if operator == "!=" and not left != right:
            return False
    return True


def _imported_modules() -> set[str]:
    """The top-level modules the application package imports."""
    imported: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(_text(path), filename=str(path))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    return imported


def _lock_via(path: Path) -> dict[str, list[str]]:
    """The reasons uv records for each locked package, as {name: [reason]}."""
    vias: dict[str, list[str]] = {}
    current = ""
    reading = False
    for raw in _text(path).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("\\"):
            # Continuation of a requirement line (its hashes).
            if not line.startswith("--"):
                pinned = line[:-1].strip()
                match = re.fullmatch(PINNED, pinned)
                if match:
                    current = _canonical(match.group(1))
                    vias[current] = []
                    reading = False
            continue
        if line.startswith("--hash="):
            continue
        match = re.fullmatch(PINNED, line)
        if match:
            current = _canonical(match.group(1))
            vias[current] = []
            reading = False
            continue
        if not line.startswith("#") or not current:
            continue
        reason = line.lstrip("#").strip()
        if reason == "via":
            reading = True
            continue
        if reason.startswith("via "):
            # Single reason uv writes inline, as ``# via pydantic``.
            reading = True
            reason = reason[len("via "):].strip()
        if not reading:
            continue
        reason = re.sub(r"\s*\(.*\)$", "", reason)
        vias[current].append(_canonical(reason))
    return vias


def _manifest_requirements(path: Path) -> dict[str, str]:
    """The requirements a manifest file declares, as {name: specifier}."""
    declared: dict[str, str] = {}
    for raw in _text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*(.*)", line)
        assert match, f"{path.relative_to(ROOT)} has an unreadable requirement: {line}"
        declared[_canonical(match.group(1))] = f"{match.group(1)}{match.group(2)}"
    return declared


def _run_commands() -> list[str]:
    """The Dockerfile's RUN commands, with line continuations joined."""
    joined = re.sub(r"\\\n", " ", _text(DOCKERFILE))
    commands = []
    for line in joined.splitlines():
        line = line.strip()
        if line.startswith("RUN "):
            commands.append(line[len("RUN "):].strip())
    return commands


# --- The runtime lock -----------------------------------------------------


def test_every_declared_dependency_is_locked_with_hashes():
    """S19: the image installs pinned, hashed versions of every dependency."""
    locked = _lock_entries(RUNTIME_LOCK)
    for name, specifier in _declared_dependencies().items():
        assert name in locked, f"{name} is not locked"
        assert _satisfies(str(locked[name]["version"]), specifier), (name, specifier)
        hashes = locked[name]["hashes"]
        assert hashes, f"{name} is locked without a hash"
        for digest in hashes:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), digest


def test_every_runtime_dependency_is_imported():
    """S4: a declared dependency the code never imports is not a dependency.

    An unused runtime dependency still ships to every owner's image, so the
    manifest has to stay a description of the code, not of former intentions.
    """
    imported = _imported_modules()
    for name in _declared_dependencies():
        module = name.replace("-", "_")
        if module in ENTRYPOINT_DEPENDENCIES:
            continue
        assert module in imported, f"{name} is declared but never imported"


def test_the_lock_covers_the_transitive_closure():
    """The lock is complete: nothing is resolved at build time."""
    locked = _lock_entries(RUNTIME_LOCK)
    for name in (
        "werkzeug", "jinja2", "itsdangerous", "click", "blinker",
        "urllib3", "certifi", "idna", "charset-normalizer",
        "pydantic-core", "annotated-types", "typing-extensions",
        "cffi", "pycparser",
    ):
        assert name in locked, f"{name} is missing from the lock"
    assert len(locked) >= 20, sorted(locked)


def test_the_lock_only_carries_dependencies_the_package_uses():
    """S4: nothing the manifest does not reach ships in the image.

    Every locked package must be reachable from a dependency declared in
    ``pyproject.toml`` through the reasons uv records (``# via``): a stale lock
    keeps installing packages whose only consumer was removed.
    """
    vias = _lock_via(RUNTIME_LOCK)
    locked = set(vias)
    # ``# via X`` on package P means P is installed because of X: the edge runs
    # from the requiring package to the required one.
    required_by: dict[str, set[str]] = {}
    for name, reasons in vias.items():
        for reason in reasons:
            required_by.setdefault(reason, set()).add(name)
    roots = required_by.get(DISTRIBUTION, set()) & locked
    assert roots, f"the lock names no dependency of {DISTRIBUTION}"
    declared = set(_declared_dependencies())
    assert roots == declared, (
        f"the lock installs {sorted(roots - declared)} and misses {sorted(declared - roots)}"
    )
    reachable = set(roots)
    pending = list(roots)
    while pending:
        for name in required_by.get(pending.pop(), set()):
            if name in locked and name not in reachable:
                reachable.add(name)
                pending.append(name)
    assert reachable == locked, (
        f"the lock carries packages the manifest does not reach: {sorted(locked - reachable)}"
    )


def test_the_lock_hashes_cover_both_architectures():
    """The locked hashes cover what linux/amd64 and linux/arm64 install."""
    locked = _lock_entries(RUNTIME_LOCK)
    for name in PLATFORM_PACKAGES:
        assert name in locked, f"{name} is missing from the lock"
        # Every distribution of the pinned version is hashed: the wheel of each
        # published architecture plus the source distribution.
        assert len(locked[name]["hashes"]) >= 4, (name, locked[name]["hashes"])
    for name, entry in locked.items():
        assert entry["hashes"], name


# --- The build toolchain lock ---------------------------------------------


def test_the_build_toolchain_is_locked_with_hashes():
    """S19: the build stage downloads nothing that is not hashed."""
    manifest = _text(BUILD_MANIFEST)
    for requirement in ("setuptools", "wheel"):
        assert requirement in manifest, requirement
    locked = _lock_entries(BUILD_LOCK)
    assert {"setuptools", "wheel"} <= locked.keys()
    for name, entry in locked.items():
        assert entry["hashes"], name
        for digest in entry["hashes"]:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), digest


def test_the_build_lock_covers_the_build_manifest():
    """The build lock agrees with the manifest that declares the toolchain."""
    locked = _lock_entries(BUILD_LOCK)
    for name, specifier in _manifest_requirements(BUILD_MANIFEST).items():
        assert name in locked, f"{name} is not in the build lock"
        if specifier:
            assert _satisfies(str(locked[name]["version"]), specifier), (name, specifier)


def test_the_build_lock_covers_the_declared_build_requirements():
    """pyproject's build-system requirements are installed from the lock."""
    requires = _pyproject()["build-system"]["requires"]
    locked = _lock_entries(BUILD_LOCK)
    for requirement in requires:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*(.*)", requirement)
        assert match, requirement
        name, specifier = match.group(1), match.group(2)
        assert _canonical(name) in locked, f"{requirement} is not in the build lock"
        assert _satisfies(str(locked[_canonical(name)]["version"]), f"{name}{specifier}"), requirement


# --- The image ------------------------------------------------------------


def _pip_invocations() -> list[str]:
    """Every pip invocation of the Dockerfile, one per command."""
    invocations = []
    for command in _run_commands():
        for part in command.split("&&"):
            part = part.strip()
            if re.search(r"(^|\s)pip\s+(install|wheel|download)\s", part):
                invocations.append(part)
    return invocations


def test_the_image_installs_only_from_the_hashed_locks():
    """S19: pip refuses to install anything the locks do not hash."""
    dockerfile = _text(DOCKERFILE)
    assert "docker/requirements.lock" in dockerfile
    assert "docker/build-requirements.lock" in dockerfile
    invocations = _pip_invocations()
    assert invocations
    for invocation in invocations:
        if "--require-hashes" in invocation:
            continue
        # Only the package built from this repository is installed without
        # hashes: it comes from this build, not from an index.
        assert "--no-deps" in invocation, invocation
        assert "almali-library-curator" in invocation or invocation.endswith(" ."), invocation
    assert any("--require-hashes -r /build-requirements.lock" in command for command in invocations)
    assert any("--require-hashes -r /requirements.lock" in command for command in invocations)
    # No unpinned build isolation: the backend comes from the hashed lock.
    assert any("--no-build-isolation" in command for command in invocations)
    # The runtime stage installs the verified wheels, never the index.
    assert "--no-index --find-links=/wheels" in dockerfile


def test_the_lock_and_the_package_declare_the_same_bounds():
    """S19: the package metadata and the image can no longer disagree."""
    for name, specifier in _declared_dependencies().items():
        assert ">=" in specifier, (name, specifier)
        assert "<" in specifier, f"{name} declares no upper bound: {specifier}"
    caps = {
        "flask": ("3.1", "4"),
        "flask-wtf": ("1.2", "2"),
        "gunicorn": ("25", "28"),
        "requests": ("2.33", "3"),
        "pydantic": ("2.12", "3"),
        "psutil": ("7.2", "8"),
        "cryptography": ("46", "51"),
    }
    declared = _declared_dependencies()
    for name, (floor, cap) in caps.items():
        specifier = declared[name]
        assert f">={floor}" in specifier, (name, specifier)
        assert f",<{cap}" in specifier, (name, specifier)


# --- Documentation ---------------------------------------------------------


def test_the_readme_documents_the_lock_refresh():
    readme = _text(README)
    assert "## Docker dependency locks" in readme
    section = readme.split("## Docker dependency locks", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "requirements.lock",
        "build-requirements.lock",
        "--require-hashes",
        "pip download",
        "uv pip compile",
        "--generate-hashes",
    ):
        assert needle in section, needle
    for platform in TARGET_PLATFORMS:
        assert platform in section, platform


# --- Optional: the lock installs on both architectures ---------------------


@pytest.mark.skipif(
    os.environ.get("ALC_LOCK_TESTS") != "1" or shutil.which("pip") is None,
    reason="set ALC_LOCK_TESTS=1 with network access to resolve the lock",
)
@pytest.mark.parametrize("platform", TARGET_PLATFORMS)
def test_the_lock_downloads_for_the_published_architecture(platform, tmp_path):
    """pip accepts the locked hashes for what each architecture installs."""
    target = tmp_path / platform
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "download",
            "--require-hashes", "--only-binary=:all:", "--no-deps",
            "--implementation", "cp", "--abi", "cp312",
            "--platform", platform,
            "--python-version", IMAGE_PYTHON,
            "-r", str(RUNTIME_LOCK),
            "-d", str(target),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    os.environ.get("ALC_LOCK_TESTS") != "1" or shutil.which("pip") is None,
    reason="set ALC_LOCK_TESTS=1 with network access to resolve the lock",
)
def test_the_lock_refuses_an_artifact_that_does_not_match(tmp_path):
    """A hash that does not match the artifact stops the install."""
    tampered = tmp_path / "tampered.lock"
    lines: list[str] = []
    inside = False
    done = False
    for line in RUNTIME_LOCK.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not done and not inside and stripped and not stripped.startswith("#"):
            inside = True
        if inside and not done:
            lines.append(re.sub(r"[0-9a-f]{64}", "0" * 64, line))
            done = not stripped.endswith("\\")
            continue
        lines.append(line)
    assert inside and done, "the lock carries no hash"
    tampered.write_text("\n".join(lines), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "download",
            "--require-hashes", "--only-binary=:all:", "--no-deps",
            "--implementation", "cp", "--abi", "cp312",
            "--platform", TARGET_PLATFORMS[0],
            "--python-version", IMAGE_PYTHON,
            "-r", str(tampered),
            "-d", str(tmp_path / "downloads"),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "hash" in (result.stdout + result.stderr).lower()
