"""Coder: fail with Claude's own explanation when it changed nothing.

Runs after "git add -A" in the crate's worktree, with Claude's last reply on stdin. If nothing
is staged, prints "OUTCOME <needs_info|already_done|blocked>" (from the reply's
"Outcome: ..." line; needs_info when it has none), "Claude made no changes" and Claude's reply,
then exits 1. The factory uses the outcome to decide what happens to the crate and its issue.

With "tests" or "test_only" as its argument (the Coder writing tests, in TDD or Test writer only
mode), it also fails when anything but test files changed, naming them. With "implement" (the
Coder writing code only) it fails when any test file changed; test setup such as package.json
counts as code there.
"""
import fnmatch
import re
import shutil
import subprocess
import sys

MODE = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
TESTS_ONLY = MODE in ("tests", "test_only")
CODE_ONLY = MODE == "implement"
# Folders and file names that hold tests, in the common languages' layouts.
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "spec", "specs", "testing", "testdata",
             "test_data", "fixtures", "__snapshots__", "e2e", "cypress", "playwright"}
TEST_NAMES = ["test_*.py", "*_test.py", "tests.py", "conftest.py", "*.test.*", "*.spec.*", "*_test.go",
              "*Test.java", "*Tests.java", "*Test.kt", "*Tests.kt", "*Test.cs", "*Tests.cs", "*_spec.rb",
              "*_test.rb", "*Test.php", "*_test.exs", "*.snap", "test_*.gd"]
# Test setup the tests may need: a test script or test dependencies, the runner's config.
TEST_SETUP = ["package.json", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "requirements-dev.txt",
              "requirements-test.txt", "jest.config.*", "vitest.config.*", "vitest.workspace.*",
              "playwright.config.*", "cypress.config.*", "karma.conf.*", ".mocharc*", "phpunit.xml*"]


def is_test_file(path: str, setup: bool = True) -> bool:
    """A test, or (with setup) test setup the tests may need."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in TEST_NAMES + (TEST_SETUP if setup else []))


reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)
staged = subprocess.run([git, "diff", "--cached", "--quiet"])
if staged.returncode == 0:  # nothing staged
    m = re.search(r"^\W*outcome\W*\s*(needs[ _-]?info|already[ _-]?done|blocked)\b", reply,
                  re.IGNORECASE | re.MULTILINE)
    outcome = re.sub(r"[ -]", "_", m.group(1).lower()).replace("needsinfo", "needs_info") \
        .replace("alreadydone", "already_done") if m else "needs_info"
    print(f"OUTCOME {outcome}", flush=True)
    print("Claude made no changes.", flush=True)
    print("Its explanation:" if reply else "It gave no explanation.", flush=True)
    if reply:
        print(reply, flush=True)
    sys.exit(1)
markers = subprocess.run([git, "diff", "--cached", "-G", r"^(<<<<<<<|>>>>>>>) ", "--name-only"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
if markers:
    print(f"Merge conflict markers are still in: {', '.join(markers)}. The Coder must resolve them.", flush=True)
    sys.exit(1)
files = subprocess.run([git, "diff", "--cached", "--name-only"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace").stdout.split()
print(f"{len(files)} file(s) changed.", flush=True)
if TESTS_ONLY:
    others = [f for f in files if not is_test_file(f)]
    if others:
        print("Set to write tests only, but Claude also changed files that aren't tests:", flush=True)
        for f in others:
            print(f"  {f}", flush=True)
        print("Send it back with a note, or set the Coder to another mode.", flush=True)
        sys.exit(1)
    print("All of them are tests or test setup.", flush=True)
if CODE_ONLY:
    # Python's bytecode caches (left in tests/ by the Tester's earlier run) aren't tests.
    tests = [f for f in files if is_test_file(f, setup=False)
             and "__pycache__" not in f.split("/") and not f.endswith((".pyc", ".pyo"))]
    if tests:
        print("Set to write implementation code only, but Claude also changed test files:", flush=True)
        for f in tests:
            print(f"  {f}", flush=True)
        print("Send it back with a note, or set the Coder to another mode.", flush=True)
        sys.exit(1)
    print("None of them are tests.", flush=True)
