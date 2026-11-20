"""Static and startup-contract gates for the Extension Host harness.

These tests do NOT launch VS Code. They prove that the documented commands
exist, that @vscode/test-electron is a real declared dependency, and that the
synthetic index/Python path are actually wired into the Extension Host and the
spawned `python -m voyager.api serve` child.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
E2E = REPO / "vscode-extension" / "e2e"


def _read(rel: str) -> str:
    return (E2E / rel).read_text(encoding="utf-8")


@pytest.fixture()
def scratch_dir():
    """Redirectable, self-cleaning scratch for the one real API subprocess."""
    base = os.environ.get("SESSIONFLOW_E2E_TEST_TMPDIR")
    if base:
        root = Path(base)
        root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="e2e-contract-", dir=str(root)))
    else:
        path = Path(tempfile.mkdtemp(prefix="e2e-contract-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_e2e_directory_has_a_real_npm_contract():
    pkg_path = E2E / "package.json"
    assert pkg_path.exists(), "README tells operators to run npm install in e2e/"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert pkg["scripts"]["test"] == "node ./run-test.mjs"
    assert pkg["scripts"]["check"] == (
        "node --check ./run-test.mjs && node --check ./suite/index.js")
    assert pkg["devDependencies"]["@vscode/test-electron"] == "3.1.0"
    assert pkg["engines"]["node"].startswith(">=")
    lock = json.loads((E2E / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"][""]["devDependencies"]["@vscode/test-electron"] == "3.1.0"


def test_launcher_passes_the_full_isolated_environment_to_host():
    src = _read("run-test.mjs")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert key in src, "launcher does not isolate " + key
    assert "VOYAGER_E2E_PYTHON" in src
    assert "E2E_VSCODE_EXECUTABLE" in src
    assert "extensionTestsEnv" in src
    assert "VOYAGER_NO_SYNC" in src


def test_extension_tests_path_points_to_a_module_file():
    src = _read("run-test.mjs")
    assert "join(HERE, 'suite', 'index.js')" in src, (
        "extensionTestsPath must name the runner module, not a directory")


def test_host_suite_applies_python_and_repo_configuration_from_environment():
    src = _read("suite/index.js")
    assert "update('pythonPath'" in src or 'update("pythonPath"' in src
    assert "update('repoPath'" in src or 'update("repoPath"' in src
    assert "VOYAGER_E2E_PYTHON" in src
    assert "VOYAGER_E2E_WORKSPACE" in src
    assert "VOYAGER_E2E_INDEX" in src


def test_documentation_does_not_promote_panel_smoke_to_three_workflow_e2e():
    readme = _read("README.md")
    assert "smoke only" in readme.lower() or "smoke-only" in readme.lower()
    assert "three read-only workflows" not in readme
    assert "NOT VERIFIED" in readme


def test_seeded_db_is_reachable_through_the_isolated_api_home(scratch_dir):
    """Startup contract: HOME redirection really points the API at our DB."""
    db = scratch_dir / "home" / ".voyager" / "index.db"
    env = {
        **os.environ,
        "HOME": str(scratch_dir / "home"),
        "USERPROFILE": str(scratch_dir / "home"),
        "APPDATA": str(scratch_dir / "home" / "AppData"),
        "LOCALAPPDATA": str(scratch_dir / "home" / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(scratch_dir / "home" / ".config"),
        "XDG_DATA_HOME": str(scratch_dir / "home" / ".local"),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "VOYAGER_NO_SYNC": "1",
    }
    seed = subprocess.run(
        [sys.executable, str(E2E / "seed_index.py"), str(db)],
        env=env, capture_output=True, text=True, timeout=120)
    assert seed.returncode == 0, seed.stdout + "\n" + seed.stderr

    api = subprocess.Popen(
        [sys.executable, "-m", "voyager.api", "serve"],
        cwd=str(REPO), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        request = json.dumps({
            "id": 1,
            "op": "overview",
            "params": {"repo": "synthetic-workspace", "hours": 1000000},
        }) + "\n"
        out, err = api.communicate(request, timeout=60)
        assert api.returncode == 0, err
        payload = json.loads(out.strip().splitlines()[0])
        assert "error" not in payload, payload
        titles = [t["title"] for t in payload["result"]["threads"]]
        assert "Auth flow" in titles and "Config migration" in titles, titles
    finally:
        if api.poll() is None:
            api.kill()
            api.wait()
