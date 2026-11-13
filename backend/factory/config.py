"""Loads factory.toml."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BACKEND_DIR / "factory.toml"


@dataclass
class StepConfig:
    """One command a machine runs. A machine runs its steps in order and stops at the first failure."""
    name: str
    command: list[str]
    cwd: str = "{worktree}"
    stdin: str | None = None
    log_format: str = "text"  # "text" or "claude-stream-json"
    timeout_seconds: float = 900.0


@dataclass
class MachineConfig:
    id: str
    name: str
    steps: list[StepConfig] = field(default_factory=list)
    prompt: str = "{text}"


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
    )


def _machine(raw: dict) -> MachineConfig:
    return MachineConfig(
        id=raw["id"],
        name=raw["name"],
        steps=[_step(s) for s in raw.get("steps", [])],
        prompt=raw.get("prompt", "{text}"),
    )


def load(path: Path = DEFAULT_CONFIG) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    base = path.parent
    ws = raw["workspace"]
    workspace = (base / ws.get("path", "../workspace")).resolve()
    intake = raw.get("intake", {})
    gh = intake.get("github", {})
    jira = intake.get("jira", {})
    machines = [_machine(m) for m in raw.get("machines", [])]
    if not machines:
        raise ValueError("factory.toml needs at least one [[machines]] entry")
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
