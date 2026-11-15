"""Refiner: pass the spec on, or hand the issue back.

stdin: Claude's reply. When its first line is "Outcome: needs info / already done / blocked",
prints OUTCOME <kind> and NO_REWORK and exits 1, so the crate is scrapped as that kind and
the issue is handed back with Claude's questions. Otherwise prints the spec and exits 0.
"""
import re
import sys

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
m = re.search(r"^\W*outcome\W*\s*(needs[ _-]?info|already[ _-]?done|blocked)\b", reply, re.IGNORECASE | re.MULTILINE)
if m:
    kind = re.sub(r"[ -]", "_", m.group(1).lower()).replace("needsinfo", "needs_info").replace("alreadydone", "already_done")
    print(f"OUTCOME {kind}", flush=True)
    print("NO_REWORK", flush=True)
    print("Claude made no changes.", flush=True)
    print("Its explanation:", flush=True)
    print(reply, flush=True)
    sys.exit(1)
if not reply:
    print("Claude gave no spec.", flush=True)
    sys.exit(1)
print("Spec for the Coder:", flush=True)
print(reply, flush=True)
