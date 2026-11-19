"""Which Claude model and effort level a machine's `claude` steps use.

Each machine can pick its own in build mode; a blank pick follows the factory-wide default,
set under the gear (Factory settings, Claude). A blank default leaves the choice to the
claude CLI (its own default model and effort). Saved in agents.json next to factory.toml.
"""
from __future__ import annotations

import json
import subprocess

from . import config

AGENTS_FILE = config.BACKEND_DIR / "agents.json"
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def clean_model(value) -> str:
    """A model id the game offers (or one typed into machines.toml/agents.json by hand), else ""."""
    text = str(value or "").strip()
    return text[:60] if text and all(c.isalnum() or c in "-._[]" for c in text) else ""


def clean_effort(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in EFFORTS else ""


def defaults() -> dict:
    """The factory-wide default: {"model": str, "effort": str} ("" = the CLI's own)."""
    try:
        raw = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {"model": clean_model(raw.get("model")), "effort": clean_effort(raw.get("effort"))}


def set_defaults(model, effort) -> dict:
    out = {"model": clean_model(model), "effort": clean_effort(effort)}
    AGENTS_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def choices() -> dict:
    """What the game's pickers list."""
    return {"models": list(MODELS), "efforts": list(EFFORTS)}


_flags: dict[str, set[str]] = {}  # claude executable -> the flags its --help lists


def _cli_flags(exe: str) -> set[str]:
    """The model/effort flags this claude CLI knows, from `claude --help` (asked once)."""
    if exe not in _flags:
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            text = (out.stdout or "") + (out.stderr or "")
        except (OSError, subprocess.SubprocessError):
            text = ""
        _flags[exe] = {f for f in ("--model", "--effort") if f in text}
    return _flags[exe]


def with_choice(resolved: list[str], model: str, effort: str) -> tuple[list[str], list[str]]:
    """resolved (a claude command with its full path first) with --model/--effort added after
    the executable, and notes on what was skipped. A flag the step already has is kept as it is."""
    flags = _cli_flags(resolved[0]) if (model or effort) else set()
    add: list[str] = []
    notes: list[str] = []
    for flag, value in (("--model", model), ("--effort", effort)):
        if not value or flag in resolved[1:]:
            continue
        if flag in flags:
            add += [flag, value]
        else:
            notes.append(f"this claude CLI has no {flag} option (update Claude Code); {value} not used")
    return [resolved[0], *add, *resolved[1:]], notes


def used_choice(resolved: list[str]) -> tuple[str, str]:
    """(model, effort) a resolved claude command runs with, "default" where it names none."""
    def flag(name: str) -> str:
        args = resolved[1:]
        for k, a in enumerate(args):
            if a == name and k + 1 < len(args):
                return args[k + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return ""
    return clean_model(flag("--model")) or "default", clean_effort(flag("--effort")) or "default"
