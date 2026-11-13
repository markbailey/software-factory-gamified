"""Loads factory.toml."""
from __future__ import annotations

import copy
import json
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

    def summary(self) -> dict:
        """What the game needs to know about this machine."""
        return {"id": self.id, "type": self.type, "name": self.name, "description": self.description,
                "sprite": self.sprite, "steps": [s.name for s in self.steps], "gate": self.gate,
                "prompt": self.prompt if self.has_prompt() else "", "has_prompt": self.has_prompt(),
                "needs": self.needs, "provides": self.provides, "unique": self.unique}

    def has_prompt(self) -> bool:
        return any("{prompt}" in (st.stdin or "") for st in self.steps)


@dataclass
class GitHubIntakeConfig:
    enabled: bool = False
    repo: str = ""
    label: str = "factory"
    poll_seconds: float = 60.0


@dataclass
class JiraIntakeConfig:
    enabled: bool = False
    base_url: str = ""
    email: str = ""
    token_env: str = "JIRA_API_TOKEN"
    jql: str = "labels = factory AND statusCategory != Done"
    poll_seconds: float = 60.0


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
    cleanup_interval_minutes: float
    keep_scrapped_hours: float
    github: GitHubIntakeConfig
    jira: JiraIntakeConfig
    db_path: Path
    scripts_dir: Path


def _step(raw: dict) -> StepConfig:
    return StepConfig(
        name=raw["name"],
        command=list(raw["command"]),
        cwd=raw.get("cwd", "{worktree}"),
        stdin=raw.get("stdin"),
        log_format=raw.get("log_format", "text"),
        timeout_seconds=float(raw.get("timeout_seconds", 900)),
        allow_failure=bool(raw.get("allow_failure", False)),
    )


def _machine(raw: dict) -> MachineConfig:
    return MachineConfig(
        id=raw["id"],
        name=raw["name"],
        steps=[_step(s) for s in raw.get("steps", [])],
        prompt=raw.get("prompt", "{text}"),
        gate=bool(raw.get("gate", False)),
        parallel=bool(raw.get("parallel", False)),
        github_review=bool(raw.get("github_review", False)),
        type=raw["id"],
        description=raw.get("description", ""),
        sprite=raw.get("sprite", raw["id"]),
        needs=list(raw.get("needs", [])),
        provides=list(raw.get("provides", [])),
        unique=bool(raw.get("unique", False)),
    )


# The line -------------------------------------------------------------------
# A line is a list of entries: {"type": "coder", "id": "coder", "name": "Coder",
# "gate": false, "prompt": "..."}. Only "type" is required; the rest override the catalogue.

def load_catalog(path: Path = CATALOG_FILE) -> tuple[dict[str, MachineConfig], list[str]]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    catalog = {t["id"]: _machine(t) for t in raw.get("types", [])}
    return catalog, list(raw.get("default_line", list(catalog)))


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
        missing = [need for need in base.needs if need not in have]
        if missing:
            what = {"worktree": "a Coder", "pr": "a Packager"}
            return f"{base.name} (machine {n}) needs {', '.join(what.get(m, m) for m in missing)} before it"
        mid = str(entry.get("id") or kind)
        if mid in seen_ids or mid in ("intake", "dock", "scrap") or mid.startswith("belt_"):
            return f"machine id {mid!r} is used twice or reserved"
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
    catalog, default_line = load_catalog()
    entries = read_line()
    if entries is None or validate_line(catalog, entries):
        if entries is not None:
            print(f"[factory] line.json is not usable ({validate_line(catalog, entries)}); using the default line")
        entries = [{"type": t} for t in default_line]
    machines = build_line(catalog, entries)
    return Config(
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
        cleanup_interval_minutes=float(raw.get("cleanup", {}).get("interval_minutes", 10)),
        keep_scrapped_hours=float(raw.get("cleanup", {}).get("keep_scrapped_hours", 24)),
        github=GitHubIntakeConfig(
            enabled=bool(gh.get("enabled", False)),
            repo=gh.get("repo", ""),
            label=gh.get("label", "factory"),
            poll_seconds=float(gh.get("poll_seconds", 60)),
        ),
        jira=JiraIntakeConfig(
            enabled=bool(jira.get("enabled", False)),
            base_url=jira.get("base_url", "").rstrip("/"),
            email=jira.get("email", ""),
            token_env=jira.get("token_env", "JIRA_API_TOKEN"),
            jql=jira.get("jql", "labels = factory AND statusCategory != Done"),
            poll_seconds=float(jira.get("poll_seconds", 60)),
        ),
        db_path=base / "factory.db",
        scripts_dir=BACKEND_DIR / "scripts",
    )
