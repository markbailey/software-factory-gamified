"""CI monitor: wait for a pull request's checks to finish.

Usage: wait_for_checks.py <pr-url> [timeout-seconds]
Exit 0 when every check passed or was skipped, or when the repo has no checks at all.
Exit 1 as soon as a check fails, or on timeout. A failed GitHub Actions job prints the end of
its log, so the Coder can see what broke when the crate is sent back.
"""
import json
import re
import shutil
import subprocess
import sys
import time

url = sys.argv[1]
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 1800
POLL = 20
LOG_LINES = 80
JOB_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
# Checks can take a few seconds to register after a push.
GRACE = 30

gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)

def print_failed_log(check: dict) -> None:
    """Print the end of a failed GitHub Actions job's log (other CI systems just get the link)."""
    m = JOB_RE.search(check.get("link", "") or "")
    if not m:
        return
    r = subprocess.run([gh, "run", "view", "--job", m.group(2), "--log-failed"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (r.stdout or r.stderr).splitlines()
    if lines:
        print(f"--- last {min(len(lines), LOG_LINES)} lines of the failed log ---", flush=True)
        for line in lines[-LOG_LINES:]:
            print(line, flush=True)


start = time.monotonic()
last_summary = ""
while True:
    r = subprocess.run([gh, "pr", "checks", url, "--json", "name,bucket,link"], capture_output=True, text=True)
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
        failed = [c for c in checks if c.get("bucket") in ("fail", "cancel")]
        if failed:
            for check in failed:
                print(f"Check failed: {check['name']} {check.get('link', '')}", flush=True)
                print_failed_log(check)
            sys.exit(1)
        if all(b in ("pass", "skipping") for b in buckets):
            print("All checks passed.", flush=True)
            sys.exit(0)

    if elapsed > timeout:
        print(f"Gave up waiting after {timeout:.0f}s.", flush=True)
        sys.exit(1)
    time.sleep(POLL)
