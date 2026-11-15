"""Loads factory.toml."""
from __future__ import annotations

import dataclasses

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BACKEND_DIR / "factory.toml"
CATALOG_FILE = BACKEND_DIR / "machines.toml"
LINE_FILE = BACKEND_DIR / "line.json"
# Why a crate is scrapped (the same kinds as triage.KINDS).
SCRAP_KINDS = ("needs_info", "already_done", "blocked", "work_failed", "rejected")


@dataclass
class StepConfig:
    """One command a machine runs. A machine runs its steps in order and stops at the first failure."""
    name: str
    command: list[str]
    cwd: str = "{worktree}"
    stdin: str | None = None
    log_format: str = "text"  # "text" or "claude-stream-json"
    timeout_seconds: float = 900.0
    allow_failure: bool = False  # log a failure and carry on instead of scrapping the crate
    first_pass_only: bool = False  # skip this step when a crate comes back for rework
    # "spec": Claude's reply from this step becomes the crate's {spec} for later machines
    # (the Refiner's sharper version of the issue). "" keeps it only as {agent_result}.
    save_result_as: str = ""
    # The step only runs while this is on: the id of one of the machine's options (ticked), or
    # a placeholder's name (not empty), e.g. "pr_template". "!name" means while it's off or
    # empty, and "a,b" means both.
    when: str = ""


# What a machine can need before it, in words for "needs X before it". A need written
# "pr|worktree" is met by either.
NEED_WORDS = {"worktree": "a Coder", "pr": "a Packager (or PRs to review from the intake)",
              "risk": "a Risk analyser"}


def missing_needs(needs: list[str], have: set[str]) -> list[str]:
    """The needs (from a machine type's `needs`) that nothing before it provides."""
    return [n for n in needs if not any(alt in have for alt in n.split("|"))]


def need_words(need: str) -> str:
    return " or ".join(NEED_WORDS.get(alt, alt) for alt in need.split("|"))


def is_agent_step(step: "StepConfig") -> bool:
    return bool(step.command) and str(step.command[0]).lower() == "claude"


@dataclass
class MachineConfig:
    id: str
    name: str
    steps: list[StepConfig] = field(default_factory=list)
    prompt: str = "{text}"
    # After its steps, a gate machine holds the crate until you approve or reject it.
    gate: bool = False
    # A parallel machine works on any number of crates at once (for waiting, like CI).
    parallel: bool = False
    # Failures and your send-backs run this same machine again (with why as {notes}), with no
    # belt needed: for a machine whose work is its own reply, like the Planner.
    rework_here: bool = False
    # A gate that also takes its decision from GitHub: an approving PR review lets the crate
    # carry on, and a "request changes" review rejects it with the review as the note.
    github_review: bool = False
    # Catalogue details, used by the game's build mode and to check a line makes sense.
    type: str = ""
    description: str = ""
    sprite: str = ""
    needs: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    unique: bool = False
    # When this machine fails, send the crate back to this earlier machine (by id) to fix it,
    # on the same branch. Empty means the scrap bin.
    on_fail: str = ""
    # A gate that may approve by itself when its steps passed (the review looks good) and the
    # crate's risk, stamped by a Risk analyser earlier on the line, is at most this level:
    # "low" or "medium". Empty means always ask you. Only types with can_auto_approve.
    auto_approve: str = ""
    can_auto_approve: bool = False
    # Scrapping: a machine only drops a crate in the scrap bin when its fail belt runs to the
    # bin (scrap_bin) and the reason is one of scrap_when (triage kinds). Any other crate it
    # can't send back is held at the machine until you run it again or scrap it.
    scrap_bin: bool = False
    scrap_to: str = "scrap"  # which of the pipeline's bins its red belt runs into
    scrap_when: list[str] = field(default_factory=lambda: list(SCRAP_KINDS))
    # Tick boxes in the game's settings for this machine ([[types.options]] in machines.toml:
    # id, label, default). Steps with when = "<id>" only run while it's ticked. The option
    # "github_review" (if there is one) sets github_review.
    option_specs: list[dict] = field(default_factory=list)
    options: dict[str, bool | str] = field(default_factory=dict)  # text options (type = "text") are str
    # A sorter runs no steps: it sends each crate out of the first of its outputs whose rule
    # matches ([{"port": "out_1", "when": "risk", "value": "high"}, ...]; see graph.RULE_KINDS).
    sorter: bool = False
    rules: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        """What the game needs to know about this machine."""
        return {"id": self.id, "type": self.type, "name": self.name, "description": self.description,
                "sprite": self.sprite, "steps": [s.name for s in self.steps], "gate": self.gate,
                "prompt": self.prompt if self.has_prompt() else "", "has_prompt": self.has_prompt(),
                "needs": self.needs, "provides": self.provides, "unique": self.unique,
                "on_fail": self.on_fail, "uses_agent": self.uses_agent(), "parallel": self.parallel,
                "rework_here": self.rework_here,
                "auto_approve": self.auto_approve, "can_auto_approve": self.can_auto_approve,
                "scrap_bin": self.scrap_bin, "scrap_when": self.scrap_when,
                "option_specs": self.option_specs, "options": self.options,
                "sorter": self.sorter, "rules": self.rules}

    def uses_agent(self) -> bool:
        """True if one of its steps runs Claude (so it pauses while Claude is unavailable)."""
        return any(is_agent_step(st) for st in self.steps)

    def has_prompt(self) -> bool:
        return any("{prompt}" in (st.stdin or "") for st in self.steps)


# What a scrapped crate's issue is labelled with when it's handed back, by scrap kind
# (triage.py). An empty label means none.
DEFAULT_HANDBACK_LABELS = {"needs_info": "needs info", "already_done": "already done", "blocked": "blocked",
                           "work_failed": "factory failed", "rejected": "factory failed"}


@dataclass
class GitHubIntakeConfig:
    enabled: bool = False
    repo: str = ""
    label: str = "factory"
    poll_seconds: float = 60.0
    # Status at the source: GitHub issues have none, so labels and the assignee stand in.
    assign: bool = True  # assign the issue to you (whoever gh is signed in as) while it's a crate
    in_progress_label: str = "in progress"  # added while it's a crate; "" for none
    handback_labels: dict = field(default_factory=lambda: dict(DEFAULT_HANDBACK_LABELS))


@dataclass
class JiraIntakeConfig:
    enabled: bool = False
    base_url: str = ""
    email: str = ""
    token_env: str = "JIRA_API_TOKEN"
    jql: str = "labels = factory AND statusCategory != Done"
    poll_seconds: float = 60.0
    # Status at the source: transitions by name (the transition's or its target status's).
    assign: bool = True  # assign the issue to you (the API token's account) while it's a crate
    pickup_transition: str = "In Progress"
    handback_transition: str = "To Do"
    shipped_transition: str = "Done"
    trigger_label: str = "factory"  # removed when handed back, so it isn't picked up again at once
    handback_labels: dict = field(default_factory=lambda: dict(DEFAULT_HANDBACK_LABELS))


GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_URL_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/*$", re.I)


def github_name(text: str) -> str:
    """ "owner/name" from what you typed: owner/name, a github.com link or a clone URL.
    "" if it's none of those."""
    text = str(text or "").strip()
    if GITHUB_NAME_RE.match(text):
        return text[:-4] if text.lower().endswith(".git") else text
    m = GITHUB_URL_RE.search(text)
    return m.group(1) if m else ""


@dataclass
class RepoConfig:
    """The repo a pipeline works on: where it lives on GitHub, its local clone (branches and
    worktrees come from it) and the branch new work starts from and PRs go into."""
    github: str  # "owner/name"; "" for a repo with no GitHub remote
    path: Path  # the local clone
    base_branch: str  # "" until it's found: the repo's default branch
    remote: str  # its clone URL ("" for none)
    is_default: bool = False  # the one in factory.toml's [workspace]

    def summary(self) -> dict:
        return {"github": self.github, "path": str(self.path), "base_branch": self.base_branch or "",
                "default": self.is_default}


@dataclass
class Config:
    host: str
    port: int
    workspace: Path
    repo: Path
    worktrees: Path
    base_branch: str
    remote: str
    allowed_commands: list[str]
    travel_seconds: float
    machines: list[MachineConfig]
    catalog: dict[str, MachineConfig]
    default_line: list[str]
    max_rework: int
    cleanup_interval_minutes: float
    keep_scrapped_hours: float
    github: GitHubIntakeConfig
    jira: JiraIntakeConfig
    db_path: Path
    scripts_dir: Path
    # PRDs for the Planning line: GitHub issues in the same repo with this label ([intake.prd]).
    prd: GitHubIntakeConfig | None = None
    # The repo in [workspace]: lines that don't name their own repo work on it.
    default_repo: RepoConfig | None = None


def resolve_repo(cfg: Config, raw) -> RepoConfig:
    """The repo a pipeline's "repo" settings name: {"github": "owner/name", "path": "folder",
    "base_branch": "main"}, each optional. Nothing set means the default repo. A GitHub repo
    with no folder is cloned into workspace/repos/<owner>/<name>; a relative folder is under the
    workspace."""
    raw = raw if isinstance(raw, dict) else {}
    default = cfg.default_repo
    github = github_name(raw.get("github", ""))
    path_text = str(raw.get("path", "") or "").strip()
    base = str(raw.get("base_branch", "") or "").strip()
    if not path_text and (not github or github.lower() == default.github.lower()):
        return dataclasses.replace(default, base_branch=base or default.base_branch)
    if path_text:
        path = Path(path_text).expanduser()
        path = path if path.is_absolute() else cfg.workspace / path
    else:
        owner, name = github.split("/", 1)
        path = cfg.workspace / "repos" / owner / name
    path = path.resolve()
    if path == default.path.resolve() and not github:
        return dataclasses.replace(default, base_branch=base or default.base_branch)
    return RepoConfig(github=github, path=path, base_branch=base,
                      remote=f"https://github.com/{github}.git" if github else "")


def _step(raw: dict) -> StepConfig:
    return StepConfig(
        name=raw["name"],
        command=list(raw["command"]),
        cwd=raw.get("cwd", "{worktree}"),
        stdin=raw.get("stdin"),
        log_format=raw.get("log_format", "text"),
        timeout_seconds=float(raw.get("timeout_seconds", 900)),
        allow_failure=bool(raw.get("allow_failure", False)),
        first_pass_only=bool(raw.get("first_pass_only", False)),
        save_result_as=str(raw.get("save_result_as", "")),
        when=str(raw.get("when", "")),
    )


def _machine(raw: dict) -> MachineConfig:
    return MachineConfig(
        id=raw["id"],
        name=raw["name"],
        steps=[_step(s) for s in raw.get("steps", [])],
        prompt=raw.get("prompt", "{text}"),
        gate=bool(raw.get("gate", False)),
        parallel=bool(raw.get("parallel", False)),
        rework_here=bool(raw.get("rework_here", False)),
        github_review=bool(raw.get("github_review", False)),
        type=raw["id"],
        description=raw.get("description", ""),
        sprite=raw.get("sprite", raw["id"]),
        needs=list(raw.get("needs", [])),
        provides=list(raw.get("provides", [])),
        unique=bool(raw.get("unique", False)),
        on_fail=str(raw.get("on_fail", "")),
        can_auto_approve=bool(raw.get("can_auto_approve", False)),
        auto_approve=_risk_level(raw.get("auto_approve", "")),
        option_specs=[_option_spec(o) for o in raw.get("options", [])],
        options={s["id"]: s["default"] for s in (_option_spec(o) for o in raw.get("options", []))},
        sorter=str(raw.get("kind", "")) == "sorter",
    )


def _option_spec(raw: dict) -> dict:
    """A tick box (default: on or off); with type = "text" a line of text (default: ""); with
    type = "choice" one of `choices` ([{value, label}], default: the first)."""
    if str(raw.get("type", "")) == "choice":
        choices = [{"value": str(c["value"]), "label": str(c.get("label", c["value"]))} for c in raw.get("choices", [])]
        values = [c["value"] for c in choices]
        default = str(raw.get("default", values[0] if values else ""))
        return {"id": str(raw["id"]), "label": str(raw.get("label", raw["id"])), "type": "choice",
                "choices": choices, "default": default if default in values else (values[0] if values else "")}
    if str(raw.get("type", "")) == "text":
        return {"id": str(raw["id"]), "label": str(raw.get("label", raw["id"])), "type": "text",
                "default": str(raw.get("default", "")), "placeholder": str(raw.get("placeholder", ""))}
    return {"id": str(raw["id"]), "label": str(raw.get("label", raw["id"])), "default": bool(raw.get("default", False))}


RISK_LEVELS = ("low", "medium", "high")


def _risk_level(value) -> str:
    """ "low" or "medium" for an auto-approve setting, else "" (never)."""
    value = str(value or "").strip().lower()
    return value if value in ("low", "medium") else ""


# The line -------------------------------------------------------------------
# A line is a list of entries: {"type": "coder", "id": "coder", "name": "Coder",
# "gate": false, "prompt": "..."}. Only "type" is required; the rest override the catalogue.

def load_catalog(path: Path = CATALOG_FILE) -> tuple[dict[str, MachineConfig], list[str], int]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    catalog = {t["id"]: _machine(t) for t in raw.get("types", [])}
    return catalog, list(raw.get("default_line", list(catalog))), int(raw.get("max_rework", 3))


def read_line(path: Path = LINE_FILE) -> list[dict] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("machines")


def write_line(entries: list[dict], path: Path = LINE_FILE) -> None:
    path.write_text(json.dumps({"machines": entries}, indent=2) + "\n", encoding="utf-8")


def validate_line(catalog: dict[str, MachineConfig], entries: list) -> str | None:
    """Return a message saying what is wrong with a line, or None if it can run."""
    if not isinstance(entries, list) or not entries:
        return "the line needs at least one machine"
    have: set[str] = set()
    seen_types: set[str] = set()
    seen_ids: set[str] = set()
    for n, entry in enumerate(entries, start=1):
        kind = entry.get("type") if isinstance(entry, dict) else None
        if kind not in catalog:
            return f"machine {n}: unknown type {kind!r}"
        base = catalog[kind]
        if base.unique and kind in seen_types:
            return f"there can only be one {base.name}"
        missing = missing_needs(base.needs, have)
        if missing:
            return f"{base.name} (machine {n}) needs {', '.join(need_words(m) for m in missing)} before it"
        mid = str(entry.get("id") or kind)
        if mid in seen_ids or mid in ("intake", "dock", "scrap") or mid.startswith(("belt_", "return_")):
            return f"machine id {mid!r} is used twice or reserved"
        on_fail = entry.get("on_fail")
        if on_fail and on_fail not in seen_ids:
            return f"{entry.get('name') or base.name} can only send crates back to a machine before it"
        seen_types.add(kind)
        seen_ids.add(mid)
        have.update(base.provides)
    return None


def build_line(catalog: dict[str, MachineConfig], entries: list[dict]) -> list[MachineConfig]:
    machines = []
    for entry in entries:
        m = copy.deepcopy(catalog[entry["type"]])
        m.id = str(entry.get("id") or m.type)
        m.name = str(entry.get("name") or m.name)[:40]
        if "gate" in entry:
            m.gate = bool(entry["gate"])
            m.github_review = m.github_review and m.gate
        if entry.get("prompt") and m.has_prompt():
            m.prompt = str(entry["prompt"])
        if "on_fail" in entry:
            m.on_fail = str(entry["on_fail"] or "")
        if "auto_approve" in entry:
            m.auto_approve = _risk_level(entry["auto_approve"])
        if not (m.can_auto_approve and m.gate):
            m.auto_approve = ""
        m.scrap_bin = bool(entry.get("scrap_bin", False))
        m.scrap_to = str(entry.get("scrap_to") or "scrap")
        if isinstance(entry.get("options"), dict):
            choices = {s["id"]: [c["value"] for c in s["choices"]] for s in m.option_specs if s.get("type") == "choice"}
            for key, value in entry["options"].items():
                if key in choices:
                    if str(value) in choices[key]:
                        m.options[key] = str(value)
                elif key in m.options:
                    m.options[key] = str(value or "").strip()[:500] if isinstance(m.options[key], str) else bool(value)
        if "github_review" in m.options:
            m.github_review = m.options["github_review"] and m.gate
        if m.sorter and isinstance(entry.get("rules"), list):
            m.rules = [{"port": str(r.get("port", "")), "when": str(r.get("when", "")),
                        "value": str(r.get("value", "") or "").strip()}
                       for r in entry["rules"] if isinstance(r, dict)]
        if isinstance(entry.get("scrap_when"), list):
            m.scrap_when = [k for k in SCRAP_KINDS if k in entry["scrap_when"]]
        if entry.get("timeout_seconds"):
            for st in m.steps:  # one limit for each of the machine's steps, set in the game
                st.timeout_seconds = float(entry["timeout_seconds"])
        if m.on_fail not in {x.id for x in machines}:
            m.on_fail = ""  # the catalogue's default target isn't earlier in this line
        machines.append(m)
    return machines


def load(path: Path = DEFAULT_CONFIG) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    base = path.parent
    ws = raw["workspace"]
    workspace = (base / ws.get("path", "../workspace")).resolve()
    intake = raw.get("intake", {})
    gh = intake.get("github", {})
    jira = intake.get("jira", {})
    catalog, default_line, max_rework = load_catalog()
    entries = read_line()
    if entries is None or validate_line(catalog, entries):
        if entries is not None:
            print(f"[factory] line.json is not usable ({validate_line(catalog, entries)}); using the default line")
        entries = [{"type": t} for t in default_line]
    machines = build_line(catalog, entries)
    cfg = Config(
        host=raw.get("server", {}).get("host", "127.0.0.1"),
        port=int(raw.get("server", {}).get("port", 8765)),
        workspace=workspace,
        repo=workspace / ws.get("repo", "test-project"),
        worktrees=workspace / ws.get("worktrees", "worktrees"),
        base_branch=ws.get("base_branch", "main"),
        remote=ws.get("remote", ""),
        allowed_commands=list(raw["security"]["allowed_commands"]),
        travel_seconds=float(raw.get("belts", {}).get("travel_seconds", 1.5)),
        machines=machines,
        catalog=catalog,
        default_line=default_line,
        max_rework=max_rework,
        cleanup_interval_minutes=float(raw.get("cleanup", {}).get("interval_minutes", 10)),
        keep_scrapped_hours=float(raw.get("cleanup", {}).get("keep_scrapped_hours", 24)),
        github=GitHubIntakeConfig(
            enabled=bool(gh.get("enabled", False)),
            repo=gh.get("repo", ""),
            label=gh.get("label", "factory"),
            poll_seconds=float(gh.get("poll_seconds", 60)),
            assign=bool(gh.get("assign", True)),
            in_progress_label=str(gh.get("in_progress_label", "in progress")),
            handback_labels={**DEFAULT_HANDBACK_LABELS, **{str(k): str(v) for k, v in
                                                          gh.get("handback_labels", {}).items()}},
        ),
        jira=JiraIntakeConfig(
            enabled=bool(jira.get("enabled", False)),
            base_url=jira.get("base_url", "").rstrip("/"),
            email=jira.get("email", ""),
            token_env=jira.get("token_env", "JIRA_API_TOKEN"),
            jql=jira.get("jql", "labels = factory AND statusCategory != Done"),
            poll_seconds=float(jira.get("poll_seconds", 60)),
            assign=bool(jira.get("assign", True)),
            pickup_transition=str(jira.get("pickup_transition", "In Progress")),
            handback_transition=str(jira.get("handback_transition", "To Do")),
            shipped_transition=str(jira.get("shipped_transition", "Done")),
            trigger_label=str(jira.get("trigger_label", "factory")),
            handback_labels={**DEFAULT_HANDBACK_LABELS, **{str(k): str(v) for k, v in
                                                          jira.get("handback_labels", {}).items()}},
        ),
        db_path=base / "factory.db",
        scripts_dir=BACKEND_DIR / "scripts",
    )
    remote = str(ws.get("remote", ""))
    cfg.default_repo = RepoConfig(github=github_name(remote) or github_name(cfg.github.repo), path=cfg.repo,
                                  base_branch=cfg.base_branch, remote=remote, is_default=True)
    if not cfg.github.repo:
        cfg.github.repo = cfg.default_repo.github
    prd = intake.get("prd", {})
    if bool(prd.get("enabled", cfg.github.enabled)):
        cfg.prd = dataclasses.replace(
            cfg.github, enabled=True, label=str(prd.get("label", "prd")),
            poll_seconds=float(prd.get("poll_seconds", cfg.github.poll_seconds)))
    return cfg
