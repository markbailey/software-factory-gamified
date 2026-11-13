"""Tester machine: find and run the project's tests in the current folder.

- JavaScript/TypeScript: if package.json has a "test" script, install dependencies with the
  project's package manager (pnpm, yarn or npm, from "packageManager" or the lockfile) and run
  its test script. In a turbo monorepo that is `turbo run test` across every package.
- Python: pytest if it is installed, otherwise unittest, for any test_*.py files.
- Both run if both exist; every one must pass. A project with no tests passes, with a note.
Exit code 0 means the tests passed.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", ".turbo"}


def run(cmd: list[str], env: dict | None = None) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=root, env=env).returncode


def package_manager(pkg: dict) -> str:
    declared = str(pkg.get("packageManager", "")).split("@")[0]
    if declared in ("pnpm", "yarn", "npm"):
        return declared
    if (root / "pnpm-lock.yaml").exists() or (root / "pnpm-workspace.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def js_tests() -> int | None:
    """Run the package.json test script. None if there isn't one."""
    pkg_file = root / "package.json"
    if not pkg_file.exists():
        return None
    try:
        pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"package.json is not valid JSON: {exc}", flush=True)
        return 1
    if "test" not in pkg.get("scripts", {}):
        return None
    pm = package_manager(pkg)
    exe = shutil.which(pm)
    if not exe:
        print(f"This project uses {pm}, but {pm} is not installed or not on PATH.", flush=True)
        return 1
    print(f"Running JavaScript tests with {pm}.", flush=True)
    code = run([exe, "install"])
    if code != 0:
        print(f"{pm} install failed (exit code {code}).", flush=True)
        return code
    return run([exe, "test"])


def python_test_files() -> list[Path]:
    found = []
    for path in root.rglob("test*.py"):
        if SKIP.intersection(path.relative_to(root).parts):
            continue
        if path.name.startswith("test_") or path.name == "tests.py":
            found.append(path)
    return found


def python_tests() -> int | None:
    tests = python_test_files()
    if not tests:
        return None
    print(f"Running Python tests ({len(tests)} file(s)).", flush=True)
    if importlib.util.find_spec("pytest"):
        cmd = [sys.executable, "-m", "pytest", "-q"]
    else:
        start = "tests" if (root / "tests").is_dir() else "."
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", start, "-v"]
    # Let tests import the project's modules whether or not tests/ is a package.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
    return run(cmd, env)


results = {name: fn() for name, fn in (("JavaScript", js_tests), ("Python", python_tests))}
ran = {name: code for name, code in results.items() if code is not None}
if not ran:
    print("No tests found. Passing, but consider asking the Coder for tests.", flush=True)
    sys.exit(0)

failed = [name for name, code in ran.items() if code != 0]
for name, code in ran.items():
    print(f"{name} tests {'passed' if code == 0 else f'failed (exit code {code})'}.", flush=True)
sys.exit(1 if failed else 0)
