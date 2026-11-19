"""Loads factory.toml."""
from __future__ import annotations

import dataclasses

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, prompts

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

BACKEND_DIR = paths.BACKEND_DIR  # beside the exe when built with build_exe.py
DEFAULT_CONFIG = BACKEND_DIR / "factory.toml"
CATALOG_FILE = BACKEND_DIR / "machines.toml"
LINE_FILE = BACKEND_DIR / "line.json"
MAX_RETRIES = 5  # most times a machine runs a failed crate again by itself
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
    # stdin_file = "coder/write_tests.md" in machines.toml: stdin comes from that file under
    # backend/prompts/, read again each run (see prompts.py); stdin holds its text at start.
    stdin_file: str = ""

    def stdin_text(self) -> str | None:
        """What the step sends on stdin now (its prompt file as it is now, if it has one)."""
        return prompts.read(self.stdin_file, self.stdin or "") if self.stdin_file else self.stdin


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
    # prompt_file = "coder/prompt.md" in machines.toml: the default prompt is that file under
    # backend/prompts/ (see prompts.py), and default_prompt is its text at start. While prompt is
    # still the default (not one you edited in the game), the file is read again on each run.
    prompt_file: str = ""
    default_prompt: str = ""
    # After its steps, a gate machine holds the crate until you approve or reject it.
    gate: bool = False
    # Whether you choose that in the game: "choice" (the "wait for my approval" tick), "always"
    # (the Approver: it's what the machine is for) or "never" (the Reviewer only reviews).
    gate_setting: str = "choice"
    # A parallel machine works on any number of crates at once (for waiting, like CI).
    parallel: bool = False
    # Failures and your send-backs run this same machine again (with why as {notes}), with no
    # belt needed: for a machine whose work is its own reply, like the Planner.
    rework_here: bool = False
    # Set in the game: when it fails, run the crate through this machine again (with why it
    # failed as {notes}) up to this many times before its red belt, a bin or holding it.
    retries: int = 0
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
    # A type the game lets you switch off (can_switch_off in machines.toml). Switched off (a
    # pipeline machine saved with "enabled": false), it runs nothing: crates go straight out of
    # its green port.
    can_switch_off: bool = False
    switched_off: bool = False
    # Only for types that can be switched off: {"match": "any" | "all", "rules": [{"when", "value"}]}
    # (graph.filter_problem). Crates that don't match pass straight through. Empty: every crate.
    filter: dict = field(default_factory=dict)
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
    # kind = "splitter": a sorter with no rules that sends crates out of its outputs in turn.
    # kind = "merger": runs no steps; crates from every belt into it carry straight on out.
    kind: str = ""
    # The prompt is only used while this holds (a show_when, e.g. "doc_kind=auto|pr"); the
    # rest of the time the game hides it and shows prompt_note (prompt_notes[value] first).
    prompt_label: str = ""
    prompt_when: str = ""
    prompt_notes: dict[str, str] = field(default_factory=dict)
    # The agent (claude, gemini, antigravity, copilot), model and effort level its agent steps
    # use, picked in the game (agents.py). "" follows the factory-wide default under the gear.
    agent: str = ""
    model: str = ""
    effort: str = ""

    def step_names(self, options: dict | None = None) -> list[str]:
        """The names of the steps the game lists: all of them, less those its settings turn off
        (e.g. a Coder in TDD mode doesn't list "Write code" or "Write tests")."""
        options = self.options if options is None else options
        return [s.name for s in self.steps if step_on_for(s.when, options)]

    def summary(self) -> dict:
        """What the game needs to know about this machine."""
        return {"id": self.id, "type": self.type, "name": self.name, "description": self.description,
                "sprite": self.sprite, "steps": self.step_names(), "gate": self.gate,
                "step_list": [{"name": s.name, "when": s.when} for s in self.steps],
                "gate_setting": self.gate_setting,
                "prompt": self.current_prompt() if self.has_prompt() else "", "has_prompt": self.has_prompt(),
                "needs": self.needs, "provides": self.provides, "unique": self.unique,
                "on_fail": self.on_fail, "uses_agent": self.uses_agent(), "parallel": self.parallel,
                "rework_here": self.rework_here,
                "auto_approve": self.auto_approve, "can_auto_approve": self.can_auto_approve,
                "can_switch_off": self.can_switch_off, "switched_off": self.switched_off,
                "can_filter": self.can_switch_off, "filter": self.filter,
                "scrap_bin": self.scrap_bin, "scrap_when": self.scrap_when,
                "option_specs": self.option_specs, "options": self.options,
                "sorter": self.sorter, "rules": self.rules, "kind": self.kind,
                "prompt_label": self.prompt_label, "prompt_when": self.prompt_when,
                "prompt_notes": self.prompt_notes, "agent": self.agent, "model": self.model, "effort": self.effort}

    def uses_agent(self) -> bool:
        """True if one of its steps runs Claude (so it pauses while Claude is unavailable)."""
        return any(is_agent_step(st) for st in self.steps)

    def has_prompt(self) -> bool:
        return any("{prompt}" in (st.stdin or "") for st in self.steps)

    def is_default_prompt(self, text: str | None = None) -> bool:
        """Whether text (default: this machine's prompt) is its type's default prompt, as it was
        at start or as its file is now."""
        text = self.prompt if text is None else text
        return prompts.same(text, self.default_prompt) or bool(
            self.prompt_file and prompts.same(text, prompts.read(self.prompt_file, self.default_prompt)))

    def current_prompt(self) -> str:
        """The prompt to use now: yours if you edited it in the game, else its file as it is now."""
        if self.prompt_file and prompts.same(self.prompt, self.default_prompt):
            return prompts.read(self.prompt_file, self.default_prompt)
        return self.prompt


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
    is_default: bool = False  # the one in factory.toml's [workspace] (a clone kept there, not a default)
    unset: bool = False  # a line that hasn't picked a repo: nothing is cloned or worked on for it

    def summary(self) -> dict:
        return {"github": self.github, "path": "" if self.unset else str(self.path),
                "base_branch": self.base_branch or "", "default": self.is_default, "unset": self.unset}


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
    # The repo in [workspace]: where that GitHub repo's clone is kept, for lines that pick it. It
    # isn't a default: a line that names no repo is paused until you pick one.
    default_repo: RepoConfig | None = None
    # [budget]: what Claude may cost (as Claude Code reports it), 0 for no limit. A crate over
    # crate_usd is held before its next Claude machine; once the day's total reaches daily_usd,
    # crates wait in front of Claude machines until midnight.
    crate_budget_usd: float = 0.0
    daily_budget_usd: float = 0.0
    # [alerts]: tell you outside the game when a crate needs you, is held or scrapped, or ships (alerts.py).
    alerts: dict = field(default_factory=dict)


def resolve_repo(cfg: Config, raw) -> RepoConfig:
    """The repo a pipeline's "repo" settings name: {"github": "owner/name", "path": "folder",
    "base_branch": "main"}, each optional. Nothing set means no repo (unset: the line is paused
    until you pick one). A GitHub repo with no folder is cloned into workspace/repos/<owner>/<name>
    (factory.toml's [workspace] repo keeps its clone where [workspace] says); a relative folder is
    under the workspace."""
    raw = raw if isinstance(raw, dict) else {}
    default = cfg.default_repo
    github = github_name(raw.get("github", ""))
    path_text = str(raw.get("path", "") or "").strip()
    base = str(raw.get("base_branch", "") or "").strip()
    if not path_text and not github:
        return RepoConfig(github="", path=cfg.workspace / "no-repo", base_branch=base, remote="", unset=True)
    if not path_text and default.github and github.lower() == default.github.lower():
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
        stdin=prompts.load(str(raw["stdin_file"])) if raw.get("stdin_file") else raw.get("stdin"),
        stdin_file=str(raw.get("stdin_file", "")),
        log_format=raw.get("log_format", "text"),
        timeout_seconds=float(raw.get("timeout_seconds", 900)),
        allow_failure=bool(raw.get("allow_failure", False)),
        first_pass_only=bool(raw.get("first_pass_only", False)),
        save_result_as=str(raw.get("save_result_as", "")),
        when=str(raw.get("when", "")),
    )


# Every machine that runs an agent gets this setting: the reference docs it reads (docs.py).
REFERENCE_DOCS_OPTION = {"id": "reference_docs", "label": "Reference docs", "type": "text", "default": "",
                         "placeholder": "docs.toml library ids, e.g. guidelines (blank: its defaults; none: none)"}


def _machine(raw: dict) -> MachineConfig:
    m = _machine_base(raw)
    if m.uses_agent() and "reference_docs" not in m.options:
        m.option_specs.append(dict(REFERENCE_DOCS_OPTION))
        m.options["reference_docs"] = ""
    return m


def _machine_base(raw: dict) -> MachineConfig:
    return MachineConfig(
        id=raw["id"],
        name=raw["name"],
        steps=[_step(s) for s in raw.get("steps", [])],
        prompt=(prompt := prompts.load(str(raw["prompt_file"])) if raw.get("prompt_file") else raw.get("prompt", "{text}")),
        prompt_file=str(raw.get("prompt_file", "")),
        default_prompt=prompt,
        gate=bool(raw.get("gate", False)) if raw.get("gate_setting") != "never" else False,
        gate_setting=raw.get("gate_setting") if raw.get("gate_setting") in ("always", "never") else "choice",
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
        can_switch_off=bool(raw.get("can_switch_off", False)),
        auto_approve=_risk_level(raw.get("auto_approve", "")),
        option_specs=[_option_spec(o) for o in raw.get("options", [])],
        options={s["id"]: s["default"] for s in (_option_spec(o) for o in raw.get("options", []))},
        sorter=str(raw.get("kind", "")) in ("sorter", "splitter"),
        kind=str(raw.get("kind", "")),
        prompt_label=str(raw.get("prompt_label", "")),
        prompt_when=str(raw.get("prompt_when", "")),
        prompt_notes={str(k): str(v) for k, v in (raw.get("prompt_notes") or {}).items()},
    )


def _option_spec(raw: dict) -> dict:
    """A tick box (default: on or off); with type = "text" a line of text (default: ""); with
    type = "choice" one of `choices` ([{value, label}], default: the first). With show_when =
    "<choice id>=<value>[|<value>...]" the game only shows it while that choice has one of them."""
    spec = _option_spec_base(raw)
    if raw.get("show_when"):
        spec["show_when"] = str(raw["show_when"])
    return spec


def _option_spec_base(raw: dict) -> dict:
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
        if "gate" in entry and m.gate_setting == "choice":
            m.gate = bool(entry["gate"])
            m.github_review = m.github_review and m.gate
        if entry.get("prompt") and m.has_prompt() and not m.is_default_prompt(str(entry["prompt"])):
            m.prompt = str(entry["prompt"])  # edited in the game (a copy of the default follows its file)
        if "on_fail" in entry:
            m.on_fail = str(entry["on_fail"] or "")
        if "auto_approve" in entry:
            m.auto_approve = _risk_level(entry["auto_approve"])
        if not (m.can_auto_approve and m.gate):
            m.auto_approve = ""
        m.switched_off = m.can_switch_off and entry.get("enabled", True) is False
        m.filter = _filter(entry.get("filter")) if m.can_switch_off else {}
        m.scrap_bin = bool(entry.get("scrap_bin", False))
        m.scrap_to = str(entry.get("scrap_to") or "scrap")
        if isinstance(entry.get("options"), dict):
            choices = {s["id"]: [c["value"] for c in s["choices"]] for s in m.option_specs if s.get("type") == "choice"}
            for key, value in entry["options"].items():
                if key == "review_type":  # a Reviewer saved with a type since merged into another
                    from .review_types import ALIASES
                    value = ALIASES.get(str(value), value)
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
        try:
            m.retries = max(0, min(MAX_RETRIES, int(entry.get("retries") or 0))) if not m.sorter and m.steps else 0
        except (TypeError, ValueError):
            m.retries = 0
        if m.uses_agent():
            from . import agents  # agents imports config
            m.agent = agents.clean_agent(entry.get("agent", m.agent))
            m.model = agents.clean_model(entry.get("model", m.model))
            m.effort = agents.clean_effort(entry.get("effort", m.effort))
        else:
            m.agent = m.model = m.effort = ""
        if entry.get("timeout_seconds"):
            for st in m.steps:  # one limit for each of the machine's steps, set in the game
                st.timeout_seconds = float(entry["timeout_seconds"])
        if "on_fail" not in entry and m.on_fail not in {x.id for x in machines}:
            m.on_fail = ""  # the catalogue's default target isn't earlier in this line
        # (a red belt you laid can lead anywhere: back for rework, or on to another machine or dock)
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
        repo=workspace / (str(ws.get("repo", "") or "").strip() or "local-repo"),
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
        crate_budget_usd=float(raw.get("budget", {}).get("crate_usd", 0) or 0),
        daily_budget_usd=float(raw.get("budget", {}).get("daily_usd", 0) or 0),
        alerts=dict(raw.get("alerts", {})),
    )
    remote = str(ws.get("remote", ""))
    # Which GitHub repo that clone is: its remote's, or [intake.github] repo's for a remote that
    # isn't on GitHub. Only lines that pick it use it.
    cfg.default_repo = RepoConfig(github=github_name(remote) or github_name(cfg.github.repo), path=cfg.repo,
                                  base_branch=cfg.base_branch, remote=remote, is_default=True)
    prd = intake.get("prd", {})
    if bool(prd.get("enabled", cfg.github.enabled)):
        cfg.prd = dataclasses.replace(
            cfg.github, enabled=True, label=str(prd.get("label", "prd")),
            poll_seconds=float(prd.get("poll_seconds", cfg.github.poll_seconds)))
    return cfg


def _filter(raw) -> dict:
    """A machine's saved filter, cleaned up: {} when it has no rules (it works on every crate)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        return {}
    rules = [{"when": str(r.get("when", "")), "value": str(r.get("value", "") or "").strip()}
             for r in raw["rules"] if isinstance(r, dict) and r.get("when")]
    if not rules:
        return {}
    return {"match": "all" if raw.get("match") == "all" else "any", "rules": rules}


def step_on_for(when: str, options: dict) -> bool:
    """Whether a step with this `when` can run with these machine options. Only terms naming one
    of the options count; placeholders (known only once a crate is there) count as on. Matches
    engine._skip_reason."""
    for term in [t.strip() for t in (when or "").split(",") if t.strip()]:
        negate = term.startswith("!")
        name = term.lstrip("!")
        wanted = None
        if "=" in name:
            name, _, wanted = name.partition("=")
            name = name.strip()
        if name not in options:
            continue
        if wanted is not None:
            on = str(options[name]).strip() in [w.strip() for w in wanted.split("|")]
        else:
            on = bool(options[name])
        if on == negate:
            return False
    return True
