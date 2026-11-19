"""Branch naming: the pattern machines name a crate's branch by (the Coder's Make branch step
makes it; Push, Open PR, the review lines and cleanup all use the same name).

Set factory-wide under the gear (Factory settings, Branch naming), saved in
backend/branch_naming.json; a line can override it in build mode (its intake settings,
"repo": {"branch_pattern": ...} in pipelines.json). Blank means the factory-wide one, and the
factory-wide default is crate/{id}: what the factory always did, so nothing changes until you
set one.

A crate's branch is named once, the first time a machine that makes or uses the branch runs,
and saved on the crate (items.branch). Changing the pattern later only affects new crates, so
a crate's branch, PR and cleanup never lose track of each other. The janitor and the
dashboard's Worktrees & branches tab recognise a crate's branch by that saved name (and
crate/<n> for crates from before this).

Placeholders (unknown ones are an error when you save):
  {id}            the crate's number
  {issue}         the issue it came from: 12 for GitHub #12, PROJ-12 for Jira; the crate's
                  number when it didn't come from an issue
  {issue_number}  just the number of the issue (12 for PROJ-12); the crate's number otherwise
  {slug}          the crate's title, lower-case-with-dashes (at most 40 characters)
  {line}          the line's name, as a slug
  {machine}       the type of the machine that makes the branch (coder)
  {source}        where the crate came from: github, jira, game, prd, custom ...
  {repo}          the repo's name (my-app for owner/my-app)
  {date}          the day it was named, 2026-11-18 ({yyyy}, {mm}, {dd} on their own)

The result is cleaned into a legal git branch name (spaces and characters git refuses become
dashes, no "..", no leading dot or trailing ".lock" in any part, ...). If it would clash with
another crate's branch or a branch already in the repo, -<crate number> is added; if it still
clashes (or comes out empty), the crate gets crate/<id>.
"""
from __future__ import annotations

import json
import re
import string
import subprocess
import time
from pathlib import Path

from .config import BACKEND_DIR

SETTINGS_FILE = BACKEND_DIR / "branch_naming.json"
DEFAULT_PATTERN = "crate/{id}"
MAX_LENGTH = 120
SLUG_LENGTH = 40
PLACEHOLDERS = ("id", "issue", "issue_number", "slug", "line", "machine", "source", "repo",
                "date", "yyyy", "mm", "dd")
SAMPLE = {"id": "42", "issue": "PROJ-12", "issue_number": "12", "slug": "add-dark-mode", "line": "build",
          "machine": "coder", "source": "jira", "repo": "my-app", "date": "2026-11-18", "yyyy": "2026",
          "mm": "11", "dd": "18"}
LEGACY_RE = re.compile(r"^crate/(\d+)$")

_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]+")  # what git never allows in a ref


def factory_pattern() -> str:
    """The factory-wide pattern (read each time: the game can change it at any moment)."""
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("pattern", "")
    except (OSError, ValueError, TypeError, AttributeError):
        value = ""
    return str(value or "").strip() or DEFAULT_PATTERN


def set_factory_pattern(pattern: str) -> None:
    pattern = str(pattern or "").strip()
    SETTINGS_FILE.write_text(json.dumps({"pattern": "" if pattern == DEFAULT_PATTERN else pattern}),
                             encoding="utf-8")


def slugify(text: str, limit: int = SLUG_LENGTH) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:limit].rstrip("-")


def sanitize(name: str) -> str:
    """name as a legal git branch name (git check-ref-format --branch), or "" if nothing's left."""
    name = _BAD.sub("-", str(name or ""))
    name = re.sub(r"\.{2,}", ".", name).replace("@{", "@-")
    parts = []
    for part in name.split("/"):
        part = part.lstrip(".-").rstrip(".")
        while part.endswith(".lock"):
            part = part[: -len(".lock")].rstrip(".")
        part = re.sub(r"-{2,}", "-", part).strip("-")
        if part:
            parts.append(part)
    name = "/".join(parts)[:MAX_LENGTH].rstrip("/.-")
    return "" if name in ("", "@", "HEAD") else name


def _fields(pattern: str) -> list[str]:
    try:
        return [f for _, f, _, _ in string.Formatter().parse(pattern) if f is not None]
    except ValueError as exc:
        raise ValueError(f"the pattern has a stray {{ or }}: {exc}") from None


def validate(pattern: str) -> str | None:
    """What's wrong with a pattern, or None. Blank is fine (the default or the factory's)."""
    pattern = str(pattern or "").strip()
    if not pattern:
        return None
    try:
        fields = _fields(pattern)
    except ValueError as exc:
        return str(exc)
    unknown = [f for f in fields if f not in PLACEHOLDERS]
    if unknown:
        return (f"{{{unknown[0]}}} isn't a placeholder; use "
                + ", ".join("{" + p + "}" for p in PLACEHOLDERS))
    if not sanitize(render(pattern, SAMPLE)):
        return "that pattern gives an empty branch name"
    return None


def render(pattern: str, values: dict) -> str:
    """The pattern with values filled in (missing ones blank), cleaned. Bad patterns give ""."""
    try:
        fields = _fields(pattern)
        safe = {f: str(values.get(f, "") or "") for f in fields}
        return sanitize(pattern.format_map(safe))
    except (ValueError, KeyError, IndexError):
        return ""


def values_for(item: dict, line_name: str = "", machine_type: str = "", repo: str = "",
               now: float | None = None) -> dict:
    t = time.localtime(now if now is not None else time.time())
    crate = str(item.get("id", ""))
    ref = str(item.get("source_ref") or "").strip() if item.get("source") in ("github", "jira") else ""
    ref = ref.rsplit("#", 1)[-1].lstrip("#")
    digits = re.findall(r"\d+", ref)
    title = (str(item.get("pr_title") or "").strip()
             or (str(item.get("text") or "").strip().splitlines() or [""])[0])
    return {"id": crate, "issue": ref or crate, "issue_number": digits[-1] if digits else crate,
            "slug": slugify(title), "line": slugify(line_name), "machine": slugify(machine_type),
            "source": slugify(str(item.get("source") or "")), "repo": slugify(repo.rsplit("/", 1)[-1]),
            "date": time.strftime("%Y-%m-%d", t), "yyyy": time.strftime("%Y", t),
            "mm": time.strftime("%m", t), "dd": time.strftime("%d", t)}


def _clashes(name: str, taken: set[str]) -> bool:
    """name is taken, or git couldn't make it next to one (feature vs feature/x)."""
    return any(name == t or t.startswith(name + "/") or name.startswith(t + "/") for t in taken)


def local_branches(repo: Path) -> set[str]:
    try:
        out = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=repo,
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def choose(pattern: str, values: dict, taken: set[str], reserved: set[str] = frozenset()) -> str:
    """The branch for a crate: the pattern filled in, made unique against taken (other crates'
    branches and the repo's), never one of reserved (the base branch)."""
    crate = values.get("id", "")
    fallback = f"crate/{crate}"
    name = render(pattern or DEFAULT_PATTERN, values)
    blocked = taken | set(reserved)
    if not name:
        return fallback
    if _clashes(name, blocked) or (LEGACY_RE.match(name) and name != fallback):
        name = sanitize(f"{name}-{crate}")
        if not name or _clashes(name, blocked):
            return fallback
    return name


def crate_of(branch: str, owners: dict[str, int]) -> int | None:
    """Which crate a branch is, from the crates' saved names (owners: name -> crate id), or the
    old crate/<n> naming."""
    if branch in owners:
        return owners[branch]
    m = LEGACY_RE.match(branch)
    return int(m.group(1)) if m else None


def owners(items: dict[int, dict]) -> dict[str, int]:
    """Branch name -> the crate that owns it. Two crates can share one: a PR's branch, taken
    in again from an Open PRs storage after its first crate shipped. The live one wins (then the
    newest), so the janitor never deletes a branch a crate on the line is working on."""
    done = ("shipped", "scrapped")
    out: dict[str, int] = {}
    for cid, it in sorted(items.items(), key=lambda kv: (kv[1].get("state") not in done, int(kv[0]))):
        if it.get("branch"):
            out[str(it["branch"])] = int(cid)
    return out
