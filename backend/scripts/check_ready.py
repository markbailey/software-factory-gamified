"""Verifier: pass a crate Claude found ready, or hand its issue back.

stdin: Claude's reply. Its first line is "Ready: <why>" or "Outcome: needs info / already done /
duplicate / blocked" with the reason after it. Anything but ready prints OUTCOME <kind> (a
duplicate counts as already done), NO_REWORK and Claude's reason, and exits 1. A reply with no
verdict passes, with a note, so a chatty answer never loses a good issue.
"""
import re
import sys

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
m = re.search(r"^\W*outcome\W*\s*(needs[ _-]?info|already[ _-]?done|duplicate|blocked|not[ _-]?needed)\b",
              reply, re.IGNORECASE | re.MULTILINE)
if m:
    word = re.sub(r"[ _-]", "", m.group(1).lower())
    kind = {"needsinfo": "needs_info", "alreadydone": "already_done", "duplicate": "already_done",
            "notneeded": "already_done", "blocked": "blocked"}[word]
    print(f"OUTCOME {kind}", flush=True)
    print("NO_REWORK", flush=True)
    print("Not ready to work on. Claude's reason:", flush=True)
    print(reply, flush=True)
    sys.exit(1)
if not reply:
    print("Claude gave no verdict.", flush=True)
    sys.exit(1)
if not re.search(r"^\W*ready\b", reply, re.IGNORECASE | re.MULTILINE):
    print("Claude gave no clear verdict; passing it on:", flush=True)
print(reply, flush=True)
