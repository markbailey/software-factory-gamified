"""Tester: commit lockfiles that the dependency install created or changed.

Only lockfiles are committed, so nothing else the tests produced ends up in the PR.
Usage: commit_lockfiles.py [attribution line for the end of the commit message]
"""
import shutil
import subprocess
import sys

LOCKFILES = ["pnpm-lock.yaml", "package-lock.json", "yarn.lock", "uv.lock", "poetry.lock"]
IDENTITY = ["-c", "user.name=Software Factory", "-c", "user.email=factory@localhost"]

git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)

status = subprocess.run([git, "status", "--porcelain", "--", *LOCKFILES], capture_output=True, text=True)
changed = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
if not changed:
    print("No lockfile changes.", flush=True)
    sys.exit(0)

subprocess.run([git, "add", "--", *changed], check=True)
attribution = sys.argv[1].strip() if len(sys.argv) > 1 else ""
message = "Update lockfile" + (f"\n\n{attribution}" if attribution else "")
subprocess.run([git, *IDENTITY, "commit", "-q", "-m", message], check=True)
print(f"Committed {', '.join(changed)}.", flush=True)
