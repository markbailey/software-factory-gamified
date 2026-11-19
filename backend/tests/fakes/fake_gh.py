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
elif cmd == "issue create":  # pretend issues, kept in issues.json, so intakes can list them
    issues = json.loads((state / "issues.json").read_text()) if (state / "issues.json").exists() else []
    n = 100 + len(issues) + 1
    title = args[args.index("--title") + 1] if "--title" in args else ""
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    body = sys.stdin.read() if "--body-file" in args else ""
    url = f"https://github.com/test/project/issues/{n}"
    issues.append({"number": n, "title": title, "body": body, "url": url, "state": "OPEN",
                   "labels": [{"name": l} for l in labels]})
    (state / "issues.json").write_text(json.dumps(issues))
    print(url)
elif cmd == "issue list":
    issues = json.loads((state / "issues.json").read_text()) if (state / "issues.json").exists() else []
    if "--label" in args:
        want = args[args.index("--label") + 1]
        issues = [i for i in issues if any(l["name"] == want for l in i["labels"])]
    print(json.dumps(issues))
elif cmd in ("issue edit", "issue view"):
    issues = json.loads((state / "issues.json").read_text()) if (state / "issues.json").exists() else []
    hit = next((i for i in issues if str(i["number"]) == args[2]), None)
    if hit and "--add-label" in args:
        hit["labels"].append({"name": args[args.index("--add-label") + 1]})
    if hit and "--remove-label" in args:
        gone = args[args.index("--remove-label") + 1]
        hit["labels"] = [l for l in hit["labels"] if l["name"] != gone]
    (state / "issues.json").write_text(json.dumps(issues))
    if cmd == "issue view":
        print(json.dumps(hit or {"number": int(args[2]), "title": "", "body": "", "url": "", "state": "OPEN",
                                 "labels": []}))
elif cmd in ("pr list", "search prs"):
    print("[]")
elif cmd == "api user":  # who it's signed in as: the factory's own token, or this machine's login
    print("factory-bot" if os.environ.get("GH_TOKEN") == "test-factory-token" else "machine-user")
