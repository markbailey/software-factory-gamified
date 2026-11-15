"""Reviewer: fail when Claude's review (on stdin) says the change needs work.

Exit 1, printing the review, when a line reads "Verdict: Request changes" (or the older
"Verdict: Needs changes"); otherwise exit 0.
"""
import re
import sys

review = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
if re.search(r"verdict\W*\s*(needs|request(ed|s)?) changes", review, re.IGNORECASE):
    print("The Reviewer asked for changes:", flush=True)
    print(review, flush=True)
    sys.exit(1)
print("The Reviewer is happy with the change." if review else "No review to check.", flush=True)
