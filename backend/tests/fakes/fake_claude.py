"""Stands in for the `claude` CLI in the backend's tests: answers each machine's prompt (read
from stdin) with a fixed reply, and as the Coder writes a small change in the current folder.

Behaviour switches (files in $FAKE_STATE):
  slow_<role>   sleep this many seconds first (role: coder, verifier, reviewer, ...)
  fail_tests    the test file the Coder writes fails while this file exists
  chatty        say this many lines of "thinking" first
  coder_fails   the next N Coder runs fail (N counts down)
  wrong_<role>  this role says the crate needs another machine (the file holds its type)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

state = Path(os.environ.get("FAKE_STATE", "."))
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
roles = {"coder": "You are the Coder machine", "verifier": "You are the Verifier",
         "refiner": "You are the Refiner", "reviewer": "Reviewer machine", "risk": "Risk analyser machine"}
role = next((r for r, words in roles.items() if words in prompt), "other")
slow = state / f"slow_{role}"
if slow.exists():
    time.sleep(float(slow.read_text() or "5"))


def reply(text: str) -> None:
    print(json.dumps({"type": "result", "subtype": "success", "result": text, "num_turns": 1,
                      "total_cost_usd": 0.01, "duration_ms": 10, "is_error": False}), flush=True)


chatty = state / "chatty"
if chatty.exists():
    for k in range(int(chatty.read_text() or "80")):
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"thought {k}"}]}}),
              flush=True)

fails = state / "coder_fails"
if role == "coder" and fails.exists() and int(fails.read_text() or "0") > 0:
    fails.write_text(str(int(fails.read_text()) - 1))
    print(json.dumps({"type": "result", "subtype": "error", "result": "I broke.", "is_error": True}), flush=True)
    sys.exit(1)

wrong = state / f"wrong_{role}"
if wrong.exists():
    reply(f"Outcome: wrong machine: {wrong.read_text().strip()}\nThis only needs its conflicts merged.")
    sys.exit(0)

if role == "coder":
    here = Path.cwd()
    for path in here.rglob("*"):  # resolve any merge conflict markers: keep both sides
        if path.is_file() and ".git" not in path.parts:
            text = path.read_text(encoding="utf-8", errors="replace")
            if "<<<<<<< " in text:
                kept = [l for l in text.splitlines() if not re.match(r"^(<<<<<<<|=======|>>>>>>>)", l)]
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    item = re.search(r"Crate #(\d+)", prompt)
    with open(here / "work.txt", "a", encoding="utf-8") as f:
        f.write(f"work {time.time()}\n")
    tests = here / "tests"
    tests.mkdir(exist_ok=True)
    flag = (state / "fail_tests").as_posix()
    (tests / "test_work.py").write_text(
        "import os, unittest\n\nclass T(unittest.TestCase):\n    def test_flag(self):\n"
        f"        self.assertFalse(os.path.exists({flag!r}))\n", encoding="utf-8")
    reply("Wrote work.txt and a test.")
elif role == "verifier":
    reply("Ready: clear enough to start.")
elif role == "refiner":
    reply("## Goal\nDo it.\n## Acceptance criteria\n- [ ] done")
elif role == "reviewer":
    reply("Verdict: Looks good\n- fine")
elif role == "risk":
    reply("Risk: Low\nSmall change.")
else:
    reply("OK")
