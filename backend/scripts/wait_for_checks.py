"""CI monitor: wait for a pull request's checks to finish.

Usage: wait_for_checks.py <pr-url> [timeout-seconds]
Exit 0 when every check passed or was skipped, or when the repo has no checks at all.
Exit 1 as soon as a check fails, or on timeout.
"""
import json
import shutil
import subprocess
import sys
import time

url = sys.argv[1]
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 1800
POLL = 20
# Checks can take a few seconds to register after a push.
GRACE = 30

gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)

start = time.monotonic()
last_summary = ""
while True:
    r = subprocess.run([gh, "pr", "checks", url, "--json", "name,bucket"], capture_output=True, text=True)
    checks = []
    if r.stdout.strip().startswith("["):
        checks = json.loads(r.stdout)
    elif r.returncode != 0 and "no checks" not in (r.stderr + r.stdout).lower():
        print(f"gh pr checks failed: {(r.stderr or r.stdout).strip()}", flush=True)
        sys.exit(1)

    elapsed = time.monotonic() - start
    if not checks:
        if elapsed >= GRACE:
            print("No CI checks are configured for this repo. Passing.", flush=True)
            sys.exit(0)
    else:
        buckets = [c.get("bucket", "") for c in checks]
        summary = ", ".join(f"{c['name']}: {c.get('bucket', '?')}" for c in checks)
        if summary != last_summary:
            print(summary, flush=True)
            last_summary = summary
        print(f"PROGRESS {sum(b in ('pass', 'skipping') for b in buckets) / len(buckets):.2f}", flush=True)
        if any(b in ("fail", "cancel") for b in buckets):
            print("A check failed.", flush=True)
            sys.exit(1)
        if all(b in ("pass", "skipping") for b in buckets):
            print("All checks passed.", flush=True)
            sys.exit(0)

    if elapsed > timeout:
        print(f"Gave up waiting after {timeout:.0f}s.", flush=True)
        sys.exit(1)
    time.sleep(POLL)
