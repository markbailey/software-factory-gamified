"""Triage: what a failed step means, and what happens next.

Two questions, asked when a step fails:

1. Is it the factory's tooling rather than the crate? (gh not signed in, git can't reach the
   remote, no network, a command that isn't installed, one of the factory's own scripts
   crashing.) Then nothing is scrapped: the crate waits at its machine, every machine that
   uses that tool pauses, the game shows a banner, and the step runs again once a check of
   the tool passes (or you press Try again, for problems no check can see).

2. Otherwise, if the crate can't go back for rework, why is it being scrapped? The kind
   decides what happens to the issue it came from (see intake.py):
     needs_info    Claude changed nothing: the spec is unclear or missing something
     already_done  Claude changed nothing: the code already does it
     blocked       Claude changed nothing: something outside the repo stops it
     work_failed   tests, CI, a review or a step kept failing
     rejected      you scrapped or rejected it at a gate
     wrong_line    a machine said it needs another kind of machine, and its line has none
                   (rolled back: the issue goes back to To Do, a PR is labelled "needs <machine>")
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KINDS = ("needs_info", "already_done", "blocked", "work_failed", "rejected", "wrong_line")
KIND_WORDS = {"needs_info": "Needs info", "already_done": "Already done", "blocked": "Blocked",
              "work_failed": "Work failed", "rejected": "Rejected", "wrong_line": "Not for this line"}

# A Claude machine's reply when the crate needs another kind of machine (see engine.WRONG_MACHINE_HINT).
WRONG_MACHINE_RE = re.compile(r"^\W*outcome\W*\s*wrong[ _-]?machine\W*\s*([a-z_][a-z0-9_ -]*)", re.I | re.M)


def needed_type(outcome: str) -> str:
    """The machine type a rolled-back crate needs, from its outcome ("wrong_machine:merger")."""
    return outcome.split(":", 1)[1] if outcome.startswith("wrong_machine:") else ""

# Printed by scripts/check_changes.py when Claude changed nothing.
OUTCOME_RE = re.compile(r"^OUTCOME\s+(needs_info|already_done|blocked)\s*$")


@dataclass
class ToolProblem:
    key: str  # which tool: gh, git, network, scripts, or a command's name
    label: str  # how the game names it ("GitHub CLI")
    reason: str  # in words, with what to do
    probe: list[str] | None  # a command that succeeds once it's fixed; None: only you can say
    probe_cwd: str = "repo"  # "repo" (the test project) or "workspace"


GH_AUTH = re.compile(r"gh auth login|not logged in|authentication required|HTTP 401|Bad credentials|"
                     r"token .*(expired|invalid|revoked)|To get started with GitHub CLI", re.I)
GIT_AUTH = re.compile(r"Authentication failed|could not read Username|Permission denied \(publickey\)|"
                      r"terminal prompts disabled|HTTP Basic: Access denied", re.I)
NETWORK = re.compile(r"Could not resolve host|getaddrinfo|ENOTFOUND|ECONNREFUSED|ECONNRESET|"
                     r"Network is unreachable|Connection (timed out|refused|reset)|"
                     r"error connecting to api\.github\.com|unable to access 'https?://|"
                     r"TLS handshake timeout|no such host", re.I)
MISSING = re.compile(r"not found on PATH|is not installed|command not found|is not recognized as an "
                     r"internal or external command|No such file or directory: '([\w.-]+)'", re.I)
NOT_ALLOWED = re.compile(r"not on the allowlist|is not allowed", re.I)
TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
# Scripts that run your project's own tests: their output is your code's, not the factory's,
# so only "not installed" and a crash of the script itself count as tooling there.
TEST_SCRIPTS = ("run_tests.py",)


def tooling_problem(argv: list[str], output: str, code: int, scripts_dir: Path) -> ToolProblem | None:
    """The tooling problem a failed step's output shows, or None if it looks like the crate's."""
    exe = Path(argv[0]).name.lower() if argv else ""
    exe = re.sub(r"\.(exe|cmd|bat)$", "", exe)
    script = next((Path(a).name for a in argv[1:] if a.endswith(".py")), "")
    is_factory_script = bool(script) and any(str(scripts_dir) in a or "{scripts}" in a for a in argv)
    runs_tests = script in TEST_SCRIPTS

    if code == -1 and NOT_ALLOWED.search(output):  # refused before it ran
        name = exe or "a command"
        return ToolProblem(name, name, f"`{name}` isn't in [security] allowed_commands in factory.toml. "
                           "Add it and restart the backend.", None)
    m = MISSING.search(output)
    if m and (code == -1 or code == 127 or exe in ("gh", "git") or is_factory_script):
        name = m.group(1) if m.lastindex and m.group(1) else exe or "a command"
        name = re.sub(r"\.(exe|cmd|bat)$", "", Path(name).name.lower())
        return ToolProblem(name, name, f"`{name}` isn't installed or isn't on the PATH.", [name, "--version"],
                           "workspace")
    if is_factory_script and TRACEBACK.search(output) and str(scripts_dir) in output:
        return ToolProblem("scripts", f"the {script} script",
                           f"The factory's {script} script crashed. Fix it, then press Try again.", None)
    if runs_tests:
        return None
    if exe == "gh" or is_factory_script:
        if GH_AUTH.search(output):
            return ToolProblem("gh", "GitHub CLI", "The GitHub CLI isn't signed in. Run `gh auth login` "
                               "in a terminal.", ["gh", "auth", "status"])
    if exe in ("git", "gh") or is_factory_script:
        if GIT_AUTH.search(output):
            return ToolProblem("git", "git", "git can't sign in to the remote. Check your Git credentials "
                               "(for example `gh auth setup-git`).", ["git", "ls-remote", "--heads", "origin"])
        if NETWORK.search(output):
            return ToolProblem("network", "the network", "Can't reach GitHub: check the internet connection.",
                               ["git", "ls-remote", "--heads", "origin"])
    return None


def tools_of(argvs: list[list[str]]) -> set[str]:
    """The tooling keys a machine's steps depend on (to hold crates while one is broken)."""
    keys: set[str] = set()
    for argv in argvs:
        if not argv:
            continue
        exe = re.sub(r"\.(exe|cmd|bat)$", "", Path(argv[0]).name.lower())
        keys.add(exe)
        if exe in ("gh", "git"):
            keys.add("network")
        if any("{scripts}" in a for a in argv):
            keys.add("scripts")
            if not any(a.endswith(TEST_SCRIPTS) for a in argv):
                keys.update({"gh", "git", "network"})  # the other scripts call gh and git
    return keys


def scrap_kind(outcome: str, rejected: bool) -> str:
    if rejected:
        return "rejected"
    return outcome if outcome in ("needs_info", "already_done", "blocked") else "work_failed"


def explanation(output: str) -> str:
    """The useful part of a failed step's output for a person: without our own markers."""
    keep = [l for l in output.strip().splitlines()
            if not OUTCOME_RE.match(l.strip()) and l.strip() not in ("Claude made no changes.", "Its explanation:")]
    return "\n".join(keep).strip()
