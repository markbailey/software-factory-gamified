"""Risk analyser: turn Claude's rating (on stdin) into the stamp on the crate.

Prints "RISK <low|medium|high> <why>", which the factory stamps on the crate, then the full
assessment. A reply with no "Risk: ..." line counts as high, so an unreadable rating never
lets a change be approved by itself.
"""
import re
import sys

text = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
m = re.search(r"^\W*risk\W*\s*(low|medium|med|high)\b", text, re.IGNORECASE | re.MULTILINE)
level = {"med": "medium"}.get(m.group(1).lower(), m.group(1).lower()) if m else "high"
rest = text[m.end():] if m else text
why = ""
for line in rest.splitlines():
    line = line.strip().lstrip("-*• ").strip()
    if line and not line.startswith("#"):
        why = line
        break
if not m:
    why = "No risk rating could be read from the assessment, so it counts as high. " + why
why = " ".join(why.split())[:240]
print(f"RISK {level} {why}".rstrip(), flush=True)
if text:
    print(text, flush=True)
