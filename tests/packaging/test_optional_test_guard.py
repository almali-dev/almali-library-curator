"""Exercise the real pytest collection/fixture paths without a working browser."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ["tests/packaging/test_ci_workflow.py", "tests/packaging/test_publish_workflow.py"]
LAYOUT = "tests/web/test_browser_layout.py"


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("target,stage", [(path, "import") for path in WORKFLOWS] + [
    (LAYOUT, "import"), (LAYOUT, "start"), (LAYOUT, "launch"),
])
def test_optional_unavailability_fails_only_when_required(target, stage, required):
    # Run in a fresh interpreter so imports and pytest's own session state are
    # genuine. The fake backend cannot spawn a browser or contact the network.
    harness = '''
import sys
from types import SimpleNamespace
import pytest

target, stage = sys.argv[1:]
class MissingOptional:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"yaml", "playwright.sync_api"}:
            raise ModuleNotFoundError("optional dependency unavailable for guard test", name=fullname)
if stage == "import":
    for name in list(sys.modules):
        if name == "yaml" or name.startswith("yaml.") or name.startswith("playwright"):
            del sys.modules[name]
    sys.meta_path.insert(0, MissingOptional())

def unavailable():
    raise RuntimeError("browser unavailable for guard test")
class Plugin:
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.argname == "chromium":
            manager = SimpleNamespace(chromium=SimpleNamespace(launch=unavailable), stop=lambda: None)
            backend = SimpleNamespace(start=unavailable if stage == "start" else lambda: manager)
            fixturedef.func.__globals__["sync_api"] = SimpleNamespace(sync_playwright=lambda: backend)

args = [target, "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-rs"]
if stage == "import":
    args.append("--collect-only")
else:
    args.extend(["-k", "test_the_measure_flags"])
sys.exit(pytest.main(args, plugins=[Plugin()]))
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ALC_REQUIRE_OPTIONAL_TESTS="1" if required else "0")
    result = subprocess.run(
        [sys.executable, "-c", harness, target, stage], cwd=ROOT,
        env=env, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    if required:
        assert result.returncode not in {0, 5}, output
        assert "ALC_REQUIRE_OPTIONAL_TESTS" in output
    else:
        assert result.returncode in {0, 5}, output
        assert "skipped" in output.lower()


def test_guard_does_not_fail_unrelated_skips(monkeypatch):
    import runpy
    from types import SimpleNamespace

    guard = runpy.run_path(str(ROOT / "tests/conftest.py"))
    monkeypatch.setenv("ALC_REQUIRE_OPTIONAL_TESTS", "1")
    report = SimpleNamespace(nodeid="tests/packaging/test_docker_container.py::test_boot", skipped=True,
                             outcome="skipped", longrepr="Docker is optional locally")
    guard["_require_optional_test"](report)
    assert report.outcome == "skipped"
