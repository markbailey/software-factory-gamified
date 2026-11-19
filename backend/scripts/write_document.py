"""Document writer: turn what the machine before it wrote into a document on GitHub.

stdin: the reply it works from ({agent_result}), then optionally a line "<<<NOTE>>>" and your
note from the game. It picks the document from what the reply is:

  a plan (a Planner reply with its ```json block of issues)
      -> issues: a tracking issue and one issue per task (create_issues.py)
  a review (first line "Verdict: ...") of the crate's pull request
      -> your review on the PR: an approval, a request for changes or a comment (post_review.py)

  set to jira, a plan
      -> Jira issues in the machine's Jira project, one per task (create_jira_issues.py)

--kind picks the document (the machine's "Document to create" setting): auto (tell from the
reply, as above; a plan becomes GitHub issues), issues, jira, review or pr. A pull request description is written by the step
before this one, so here "pr" only fails for a crate that has no code to describe.
Anything else fails with what it got, so the crate leaves by the red port. A new kind of
document is a new entry in KINDS: a test on the reply and the script that writes it.

Usage: write_document.py [--kind auto|issues|jira|review|pr] --pr-url URL --head-sha SHA --repo R --label L --plans DIR
                         --crate ID --source S --source-ref REF
                         [--jira-project KEY --jira-type T --jira-label L --jira-url U --jira-email E --jira-token-env V]
"""
import argparse
import subprocess
import sys
from pathlib import Path

from plan_format import split

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--kind", default="auto")
ap.add_argument("--pr-url", default="")
ap.add_argument("--head-sha", default="")
ap.add_argument("--repo", default="")
ap.add_argument("--label", default="")
ap.add_argument("--plans", default="")
ap.add_argument("--crate", default="")
ap.add_argument("--source", default="")
ap.add_argument("--source-ref", default="")
ap.add_argument("--jira-project", default="")
ap.add_argument("--jira-type", default="Task")
ap.add_argument("--jira-label", default="")
ap.add_argument("--jira-url", default="")
ap.add_argument("--jira-email", default="")
ap.add_argument("--jira-token-env", default="FACTORY_JIRA_TOKEN")
args = ap.parse_args()

raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
reply, _, note = raw.partition("<<<NOTE>>>")
reply, note = reply.strip(), note.strip()


def is_plan(text: str) -> bool:
    try:
        split(text)
        return True
    except ValueError:
        return False


def is_review(text: str) -> bool:
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first.strip().lower().lstrip("#*_ ").startswith("verdict")


def issues() -> int:
    if not args.repo:
        print("No repo to create issues in: set [intake.github] repo in factory.toml.", flush=True)
        return 1
    return run([sys.executable, str(HERE / "create_issues.py"), "--repo", args.repo, "--label", args.label,
                "--plans", args.plans, "--crate", args.crate, "--source", args.source,
                "--source-ref", args.source_ref], reply)


def jira_issues() -> int:
    return run([sys.executable, str(HERE / "create_jira_issues.py"), "--project", args.jira_project,
                "--type", args.jira_type, "--label", args.jira_label, "--site", args.jira_url,
                "--email", args.jira_email, "--token-env", args.jira_token_env,
                "--plans", args.plans, "--crate", args.crate], reply)


def review() -> int:
    if not args.pr_url:
        print("This crate has no pull request to post the review on.", flush=True)
        return 1
    return run([sys.executable, str(HERE / "post_review.py"), args.pr_url, args.head_sha],
               reply + ("\n<<<NOTE>>>\n" + note if note else ""))


# (setting, name, does the reply call for it, write it, tried on Auto). On Auto, checked in
# order; the first match wins.
KINDS = [
    ("issues", "issues from the plan", is_plan, issues, True),
    ("jira", "Jira issues from the plan", is_plan, jira_issues, False),
    ("review", "a review on the pull request", is_review, review, True),
]
WANTS = {"issues": "a Planner's plan, with its ```json block of issues",
         "jira": "a Planner's plan, with its ```json block of issues",
         "review": "a review starting \"Verdict:\""}


def run(argv: list[str], stdin: str) -> int:
    r = subprocess.run(argv, input=stdin.encode("utf-8"), cwd=str(HERE))
    return r.returncode


if not reply:
    print("Nothing to write: the machine before this one gave no reply.", flush=True)
    sys.exit(1)
kind = (args.kind or "auto").strip().lower()
if kind == "pr":
    print("This machine is set to write a pull request description, but this crate has no code of "
          "its own to describe. Put it in front of a Packager, after a Coder.", flush=True)
    sys.exit(1)
for setting, name, matches, write, on_auto in KINDS:
    if kind != setting and not (kind == "auto" and on_auto):
        continue
    if matches(reply):
        print(f"Writing {name}.", flush=True)
        sys.exit(write())
    if kind == setting:
        print(f"This machine is set to write {name}, but the machine before it didn't give "
              f"{WANTS[setting]}. It got:\n\n" + reply[:1500], flush=True)
        sys.exit(1)
print("The reply isn't a document this machine knows how to write (a plan with its issues, or a "
      "review starting \"Verdict:\"). It got:\n\n" + reply[:1500], flush=True)
sys.exit(1)
