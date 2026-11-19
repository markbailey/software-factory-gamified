"""Coder, TDD mode: run the tests Claude just wrote, before the code exists. They must fail.

Runs run_tests.py (the Tester's runner, which also installs JavaScript dependencies) in the
crate's worktree and shows its output. Exit 0 when the tests fail, as they should before the
code is written; exit 1 when they pass or there are none, since then they don't test the new
behaviour (or the spec is already done).
"""
import subprocess
import sys
from pathlib import Path

runner = Path(__file__).resolve().parent / "run_tests.py"
result = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
output = (result.stdout or "") + (result.stderr or "")
print(output.rstrip(), flush=True)
if result.returncode != 0:
    print("\nThe new tests fail, as they should before the code is written. On to the code.", flush=True)
    sys.exit(0)
if "No tests found" in output:
    print("\nNo tests found: test first needs tests the factory can run (see the Tester's runner).", flush=True)
else:
    print("\nThe new tests already pass, before any code was written, so they don't check the change. "
          "Either the spec is already done or the tests need to be stricter. Send it back with a note.",
          flush=True)
sys.exit(1)
