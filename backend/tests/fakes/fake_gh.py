"""Stands in for the `gh` CLI in the backend's tests: opens and merges pretend pull requests."""
import json
import os
import sys
from pathlib import Path

state = Path(os.environ.get("FAKE_STATE", "."))
args = sys.argv[1:]
with open(state / "gh.log", "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")
cmd = " ".join(args[:2])
if cmd == "pr create":
    n = int((state / "prn").read_text()) + 1 if (state / "prn").exists() else 1
    (state / "prn").write_text(str(n))
    print(f"https://github.com/test/project/pull/{n}")
elif cmd == "pr view":
    n = Path(args[2]).name if len(args) > 2 else "0"
    if "mergeStateStatus" in " ".join(args):
        merged = (state / f"merged_{n}").exists()
        print(json.dumps({"state": "MERGED" if merged else "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
                          "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "autoMergeRequest": None,
                          "statusCheckRollup": []}))
    else:
        print(json.dumps({"state": "MERGED" if (state / f"merged_{n}").exists() else "OPEN", "latestReviews": []}))
elif cmd == "pr merge":
    (state / f"merged_{Path(args[2]).name}").touch()
    print("Merged")
elif cmd == "pr checks":
    print(json.dumps([{"name": "build", "bucket": "pass", "link": ""}]))
elif cmd in ("pr list", "search prs", "issue list"):
    print("[]")
