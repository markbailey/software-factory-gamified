"""The attribution lines for the factory's commits and pull requests, from Claude Code's settings.

Claude Code's settings files can hold

    "attribution": {"commit": "...", "pr": "..."}

and the factory uses the same text: "commit" goes at the end of every commit message it makes,
and "pr" at the end of every pull request body it opens. The files are read the way Claude
Code reads them, highest priority first:

    managed (organisation) policy   C:\\ProgramData\\ClaudeCode\\managed-settings.json (Windows),
                                    /Library/Application Support/ClaudeCode/managed-settings.json
                                    (macOS), /etc/claude-code/managed-settings.json (Linux)
    the project's local settings    <repo>/.claude/settings.local.json
    the project's shared settings   <repo>/.claude/settings.json
    your user settings              ~/.claude/settings.json (or $CLAUDE_CONFIG_DIR/settings.json)

The first file that sets a key wins, key by key. An empty string means "no attribution". The
older "includeCoAuthoredBy": false turns both off when no attribution is set. With nothing
set anywhere, DEFAULT is used. The files are read again for every crate, so a change applies
to the next crate without restarting the backend.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def _managed_file() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _user_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "settings.json"


def settings_files(repo: Path) -> list[Path]:
    """Claude Code's settings files, highest priority first."""
    return [_managed_file(), repo / ".claude" / "settings.local.json", repo / ".claude" / "settings.json",
            _user_file()]


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(repo: Path) -> dict[str, str]:
    """{"commit": text, "pr": text}; "" means no attribution line."""
    found: dict[str, str] = {}
    co_authored: bool | None = None
    for path in settings_files(repo):
        data = _read(path)
        attribution = data.get("attribution")
        if isinstance(attribution, dict):
            for key in ("commit", "pr"):
                if key not in found and isinstance(attribution.get(key), str):
                    found[key] = attribution[key]
        if co_authored is None and isinstance(data.get("includeCoAuthoredBy"), bool):
            co_authored = data["includeCoAuthoredBy"]
    fallback = "" if co_authored is False else DEFAULT
    return {key: found.get(key, fallback).strip() for key in ("commit", "pr")}


def with_line(text: str, line: str) -> str:
    """text with the attribution line after a blank line (unchanged if there's none, or it's
    already there)."""
    text = text.rstrip()
    if not line or line in text:
        return text
    return f"{text}\n\n{line}"
