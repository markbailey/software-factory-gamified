"""Which coding agent (Claude Code, Gemini CLI, Antigravity CLI or GitHub Copilot CLI) a machine's
agent steps run, with which model and effort level.

machines.toml writes every agent step as a `claude -p ...` command (its --allowedTools and
--permission-mode say what the step may do). For a machine set to another agent, that command is
translated into the other CLI's headless command here, and its output read by that agent's parser,
so every machine and pipeline works with any of them.

Each machine can pick its own agent, model and effort in build mode; a blank pick follows the
factory-wide default, set under the gear (Factory settings, Agents). A blank model or effort leaves
the choice to the CLI. The default's model and effort apply only to machines on the default agent.
Saved in agents.json next to factory.toml.

What each CLI can do headless (checked against their docs, November 2026):
- Claude Code: `claude -p`, stream-json with cost in USD, --model, --effort, tool allowlists.
- Gemini CLI: `gemini` with the prompt on stdin, --output-format stream-json (tokens, no cost),
  -m, --approval-mode (auto_edit for edits), --allowed-tools for shell commands. No effort flag.
- Antigravity CLI (`agy`): --input-format/--output-format stream-json (prompt as a JSON event on
  stdin), --model, --effort. Tokens, no cost. Tool permissions only from its settings.json
  (permissions.allow) or --dangerously-skip-permissions, which this uses only when you turn on
  "antigravity_skip_permissions" in agents.json. Without it, edits it isn't allowed are skipped.
- GitHub Copilot CLI: `copilot` with the prompt piped in, --output-format json (JSONL events),
  --model, --allow-tool (read, write, shell(git log:*)), --no-ask-user. No effort flag (it is a
  settings.json option) and no cost (it bills in premium requests).
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass, field

from . import config

AGENTS_FILE = config.BACKEND_DIR / "agents.json"
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_AGENT = "claude"


@dataclass
class Provider:
    id: str
    label: str
    exe: str
    models: tuple = ()
    efforts: tuple = ()
    discovered: list = field(default_factory=list)  # models the CLI listed (agy models, copilot --help)

    def model_list(self) -> list[str]:
        out = list(self.models)
        out += [m for m in self.discovered if m not in out]
        return out


PROVIDERS: dict[str, Provider] = {
    "claude": Provider("claude", "Claude Code", "claude", MODELS, EFFORTS),
    "gemini": Provider("gemini", "Gemini CLI", "gemini", ("auto", "pro", "flash", "flash-lite")),
    "antigravity": Provider("antigravity", "Antigravity CLI", "agy", (), EFFORTS),
    "copilot": Provider("copilot", "GitHub Copilot CLI", "copilot"),
}


def clean_agent(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in PROVIDERS else ""


def clean_model(value) -> str:
    """A model id the game offers (or one typed into machines.toml/agents.json by hand), else ""."""
    text = str(value or "").strip()
    return text[:60] if text and all(c.isalnum() or c in "-._[]" for c in text) else ""


def clean_effort(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in EFFORTS else ""


def _raw() -> dict:
    try:
        raw = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return raw if isinstance(raw, dict) else {}


def defaults() -> dict:
    """The factory-wide default: {"agent": str, "model": str, "effort": str} ("" = the CLI's own)."""
    raw = _raw()
    return {"agent": clean_agent(raw.get("agent")) or DEFAULT_AGENT,
            "model": clean_model(raw.get("model")), "effort": clean_effort(raw.get("effort"))}


def set_defaults(model, effort, agent=None) -> dict:
    raw = _raw()
    out = {**raw, "agent": clean_agent(agent if agent is not None else raw.get("agent")) or DEFAULT_AGENT,
           "model": clean_model(model), "effort": clean_effort(effort)}
    AGENTS_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return defaults()


def skip_antigravity_permissions() -> bool:
    return _raw().get("antigravity_skip_permissions") is True


def resolve_choice(agent: str, model: str, effort: str) -> tuple[str, str, str]:
    """A machine's (agent, model, effort) with its blanks filled from the factory default."""
    d = defaults()
    agent = clean_agent(agent) or d["agent"]
    if agent == d["agent"]:
        return agent, model or d["model"], effort or d["effort"]
    return agent, model, effort


def choices() -> dict:
    """What the game's pickers list. models/efforts: Claude's (older games read these)."""
    _discover_soon()
    return {"models": list(MODELS), "efforts": list(EFFORTS),
            "agents": [{"id": p.id, "label": p.label, "models": p.model_list(), "efforts": list(p.efforts)}
                       for p in PROVIDERS.values()]}


# --- Asking the CLIs what they offer ----------------------------------------------------------

_flags: dict[str, set[str]] = {}  # executable -> the flags its --help lists
_help_text: dict[str, str] = {}
_KNOWN_FLAGS = ("--model", "--effort", "--output-format", "--input-format", "--approval-mode",
                "--allowed-tools", "--allow-tool", "--no-ask-user", "--no-color", "stream-json")


def _help(exe: str) -> str:
    if exe not in _help_text:
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            _help_text[exe] = (out.stdout or "") + (out.stderr or "")
        except (OSError, subprocess.SubprocessError):
            _help_text[exe] = ""
    return _help_text[exe]


def _cli_flags(exe: str) -> set[str]:
    """The flags this CLI knows (of the ones the factory uses), from `<cli> --help` (asked once)."""
    if exe not in _flags:
        text = _help(exe)
        _flags[exe] = {f for f in _KNOWN_FLAGS if f in text}
    return _flags[exe]


_discovery_started = False


def _discover_soon() -> None:
    """Once per backend, in the background: the models Antigravity and Copilot say they have."""
    global _discovery_started
    if _discovery_started:
        return
    _discovery_started = True
    threading.Thread(target=_discover, daemon=True).start()


_MODEL_TOKEN = re.compile(r"\b([a-z][a-z0-9]*(?:[-.][a-z0-9]+)+)\b")


def _discover() -> None:
    import shutil
    agy = PROVIDERS["antigravity"]
    path = shutil.which(agy.exe)
    if path:
        try:
            out = subprocess.run([path, "models"], capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL).stdout or ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        agy.discovered = _models_in(out)
    copilot = PROVIDERS["copilot"]
    path = shutil.which(copilot.exe)
    if path:
        # Its --help lists the --model choices: (choices: "gpt-5", "claude-sonnet-4.5", ...)
        m = re.search(r"--model[^\n]*(?:\n[^\n-][^\n]*)*?choices:([^)]*)\)", _help(path))
        copilot.discovered = re.findall(r'"([^"]+)"', m.group(1)) if m else []


def _models_in(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        m = _MODEL_TOKEN.search(line.strip().lower())
        if m and clean_model(m.group(1)) and m.group(1) not in out:
            out.append(m.group(1))
    return out[:30]


# --- Claude ------------------------------------------------------------------------------------

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


def _flag_value(args: list[str], name: str) -> str:
    for k, a in enumerate(args):
        if a == name and k + 1 < len(args):
            return args[k + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""


def used_choice(resolved: list[str]) -> tuple[str, str]:
    """(model, effort) a resolved command runs with, "default" where it names none."""
    args = resolved[1:]
    model = _flag_value(args, "--model") or _flag_value(args, "-m")
    return clean_model(model) or "default", clean_effort(_flag_value(args, "--effort")) or "default"


# --- Translating a claude step for another agent -------------------------------------------------

@dataclass
class StepNeeds:
    """What a claude step in machines.toml lets Claude do."""
    edits: bool = False  # Edit/Write, or --permission-mode acceptEdits
    shell: list[str] = field(default_factory=list)  # "git log", "python -m pytest", ...
    any_shell: bool = False  # plain Bash


def step_needs(argv: list[str]) -> StepNeeds:
    args = argv[1:]
    tools = [t.strip() for t in _flag_value(args, "--allowedTools").split(",") if t.strip()]
    needs = StepNeeds(edits=_flag_value(args, "--permission-mode") in ("acceptEdits", "bypassPermissions"))
    for t in tools:
        if t in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            needs.edits = True
        elif t == "Bash":
            needs.any_shell = True
        else:
            m = re.fullmatch(r"Bash\((.+?)(?::\*)?\)", t)
            if m:
                needs.shell.append(m.group(1).strip())
    return needs


def translate(agent: str, argv: list[str], stdin_text: str | None, model: str, effort: str,
              flags: set[str] | None = None) -> tuple[list[str], str | None, list[str]]:
    """A claude step's command (argv[0] == "claude") for another agent: (argv, stdin, notes).
    flags: the flags that CLI's --help lists (asked for when None)."""
    p = PROVIDERS[agent]
    flags = _cli_flags(p.exe) if flags is None else flags
    needs = step_needs(argv)
    notes: list[str] = []
    out = [p.exe]

    def opt(flag: str, *values: str, why: str = "") -> bool:
        if flag in flags:
            out.extend([flag, *values])
            return True
        if why:
            notes.append(f"this {p.label} has no {flag} option ({why})")
        return False

    if agent == "gemini":
        opt("--output-format", "stream-json" if "stream-json" in flags else "json")
        if needs.edits or needs.any_shell:
            opt("--approval-mode", "yolo" if needs.any_shell else "auto_edit",
                why="update Gemini CLI; it can't make edits headless without it")
        if needs.shell:
            opt("--allowed-tools", ",".join(f"run_shell_command({s})" for s in needs.shell),
                why=f"it won't run {', '.join(needs.shell)}")
        if model:
            out += ["-m", model]
        if effort:
            notes.append(f"Gemini CLI has no effort setting; {effort} not used")
    elif agent == "antigravity":
        opt("--output-format", "stream-json")
        if opt("--input-format", "stream-json") and stdin_text is not None:
            stdin_text = json.dumps({"event": "user", "message": {"content": stdin_text}}) + "\n"
        elif stdin_text is not None:
            out += ["-p", stdin_text]
            stdin_text = None
        if needs.edits or needs.shell or needs.any_shell:
            if skip_antigravity_permissions():
                out.append("--dangerously-skip-permissions")
            else:
                notes.append("Antigravity CLI only edits files and runs commands its settings.json allows "
                             "(permissions.allow, e.g. write_file(.) and command(git)); set "
                             "\"antigravity_skip_permissions\": true in agents.json to allow everything")
        if model:
            out += ["--model", model]
        if effort:
            opt("--effort", effort, why=f"update the Antigravity CLI; {effort} not used")
    elif agent == "copilot":
        opt("--output-format", "json")
        opt("--no-ask-user")
        opt("--no-color")
        allow = ["read"] + (["write"] if needs.edits else [])
        allow += ["shell"] if needs.any_shell else [f"shell({s}:*)" for s in needs.shell]
        opt("--allow-tool", ",".join(allow), why="update GitHub Copilot CLI")
        if model:
            out += ["--model", model]
        if effort:
            notes.append(f"Copilot CLI has no effort option (set effortLevel in its settings.json); {effort} not used")
    return out, stdin_text, notes


# --- Reading each agent's output -----------------------------------------------------------------

TOOL_DETAIL_KEYS = ("file_path", "path", "absolute_path", "pattern", "command", "url")


@dataclass
class Result:
    text: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    tokens: int = 0
    error: str = ""


def _detail(args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:120]
    if not isinstance(args, dict):
        return ""
    return next((str(args[k])[:160] for k in TOOL_DETAIL_KEYS if k in args), "")


def _tokens(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "totalTokens"):
        if isinstance(usage.get(key), (int, float)):
            return int(usage[key])
    return int(sum(v for k, v in usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and re.search(r"(input|output|thinking)_?tokens", k, re.I)))


class Parser:
    """Turns one agent step's output into log lines, and its final answer into a Result."""
    name = "agent"

    def __init__(self) -> None:
        self.result: Result | None = None
        self._plain: list[str] = []

    def feed(self, line: str) -> list[str]:
        try:
            msg = json.loads(line)
        except ValueError:
            if line.strip():
                self._plain.append(line)
                return [line]
            return []
        return self.event(msg) if isinstance(msg, dict) else []

    def event(self, msg: dict) -> list[str]:
        return []

    def finish(self) -> Result | None:
        """After the CLI exits: its Result (a plain-text answer when it printed no result event)."""
        if self.result is None and self._plain and not isinstance(self, ClaudeParser):
            self.result = Result(text="\n".join(self._plain).strip())
        return self.result


class ClaudeParser(Parser):
    name = "claude"

    def event(self, msg: dict) -> list[str]:
        from .formats import claude_stream_json
        if msg.get("type") == "result":
            self.result = Result(text=str(msg.get("result", "")).strip(),
                                 cost_usd=float(msg.get("total_cost_usd") or 0),
                                 turns=int(msg.get("num_turns") or 0), tokens=_tokens(msg.get("usage")))
        return claude_stream_json(json.dumps(msg))


class GeminiParser(Parser):
    """gemini --output-format stream-json: init, message (role, content, delta), tool_use,
    tool_result, error, result (status, stats). Or one json object with response and stats."""
    name = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self.text = ""  # the assistant's words since its last tool call
        self.turns = 0

    def _flush(self) -> list[str]:
        return [t for t in self.text.splitlines() if t.strip()]

    def event(self, msg: dict) -> list[str]:
        kind = msg.get("type")
        if kind is None and "response" in msg:  # --output-format json
            self.result = Result(text=str(msg.get("response") or "").strip(), tokens=_stats_tokens(msg.get("stats")),
                                 error=str((msg.get("error") or {}).get("message", "")) if msg.get("error") else "")
            return [t for t in self.result.text.splitlines() if t.strip()]
        if kind == "init":
            return [f"[gemini] session started (model {msg.get('model', 'unknown')})"]
        if kind == "message" and msg.get("role") == "assistant":
            if not self.text:
                self.turns += 1
            self.text += str(msg.get("content", ""))
            return []
        if kind == "tool_use":
            out = self._flush()
            self.text = ""
            return out + [f"  > {msg.get('tool_name', 'tool')} {_detail(msg.get('parameters'))}".rstrip()]
        if kind == "error":
            return [f"[gemini] {msg.get('severity', 'error')}: {msg.get('message', '')}"]
        if kind == "result":
            stats = msg.get("stats") or {}
            err = msg.get("error") or {}
            self.result = Result(text=self.text.strip(), turns=self.turns, tokens=_stats_tokens(stats),
                                 error=str(err.get("message", "")) if isinstance(err, dict) else str(err))
            parts = [f"[gemini] {msg.get('status', 'finished')}"]
            if self.result.tokens:
                parts.append(f"{self.result.tokens} tokens")
            if isinstance(stats, dict) and stats.get("duration_ms"):
                parts.append(f"{stats['duration_ms'] / 1000:.0f}s")
            out = self._flush() + [", ".join(parts)]
            if self.result.error:
                out.append(f"[gemini] error: {self.result.error}")
            return out
        return []


def _stats_tokens(stats) -> int:
    if not isinstance(stats, dict):
        return 0
    found = _tokens(stats)
    if found:
        return found
    models = stats.get("models") or {}  # {"gemini-x": {"tokens": {"total": n}}}
    total = 0
    for m in models.values() if isinstance(models, dict) else []:
        tok = m.get("tokens") if isinstance(m, dict) else None
        if isinstance(tok, dict) and isinstance(tok.get("total"), (int, float)):
            total += int(tok["total"])
    return total


class AntigravityParser(Parser):
    """agy --output-format stream-json: init, step_update (step_type, state, text_delta, tool_name,
    usage), result (status, response, usage, num_turns, error)."""
    name = "antigravity"

    def __init__(self) -> None:
        super().__init__()
        self.texts: dict = {}

    def event(self, msg: dict) -> list[str]:
        kind = msg.get("type") or msg.get("event")
        if kind is None and "response" in msg and "status" in msg:
            kind = "result"
        if kind == "init":
            return [f"[antigravity] session started (model {msg.get('model', 'default')})"]
        if kind == "step_update":
            step = msg.get("step_index")
            stype = msg.get("step_type")
            if stype == "agent_response":
                self.texts[step] = self.texts.get(step, "") + str(msg.get("text_delta") or "")
                if msg.get("state") == "DONE":
                    return [t for t in self.texts.get(step, "").splitlines() if t.strip()]
            elif stype == "tool" and msg.get("state") == "DONE":
                return [f"  > {msg.get('tool_name', 'tool')} {_detail(msg.get('tool_info'))}".rstrip()]
            return []
        if kind == "result":
            status = str(msg.get("status", "finished"))
            self.result = Result(text=str(msg.get("response") or "").strip(), turns=int(msg.get("num_turns") or 0),
                                 tokens=_tokens(msg.get("usage")),
                                 error="" if status == "SUCCESS" else str(msg.get("error") or status))
            parts = [f"[antigravity] {status.lower()}"]
            if self.result.turns:
                parts.append(f"{self.result.turns} turns")
            if self.result.tokens:
                parts.append(f"{self.result.tokens} tokens")
            if msg.get("duration_seconds"):
                parts.append(f"{float(msg['duration_seconds']):.0f}s")
            out = [", ".join(parts)]
            if self.result.error:
                out.append(f"[antigravity] error: {self.result.error}")
            return out
        return []


class CopilotParser(Parser):
    """copilot --output-format json (JSONL): assistant.message (data.content, data.toolRequests),
    tool.execution_start (data.toolName, data.arguments), tool.execution_complete, result
    (exitCode, usage). Anything with token counts in it adds to the total."""
    name = "copilot"

    def __init__(self) -> None:
        super().__init__()
        self.last = ""
        self.turns = 0
        self.tokens = 0

    def event(self, msg: dict) -> list[str]:
        kind = str(msg.get("type", ""))
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        if kind != "result":
            self.tokens += _tokens(data.get("usage") if "usage" in data else data if "usage" in kind else None)
        if kind == "assistant.message":
            self.turns += 1
            content = str(data.get("content") or "")
            if content.strip():
                self.last = content
            return [t for t in content.splitlines() if t.strip()]
        if kind == "tool.execution_start":
            return [f"  > {data.get('toolName', 'tool')} {_detail(data.get('arguments'))}".rstrip()]
        if kind == "tool.execution_complete" and data.get("success") is False:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            return [f"  ! {err.get('message', 'tool failed')}"]
        if kind == "result":
            self.tokens += _tokens(msg.get("usage"))
            code = msg.get("exitCode", 0)
            self.result = Result(text=self.last.strip(), turns=self.turns, tokens=self.tokens,
                                 error="" if code in (0, None) else f"exit code {code}")
            parts = ["[copilot] finished" if not self.result.error else f"[copilot] {self.result.error}"]
            if self.turns:
                parts.append(f"{self.turns} turns")
            if self.tokens:
                parts.append(f"{self.tokens} tokens")
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            if usage.get("sessionDurationMs"):
                parts.append(f"{usage['sessionDurationMs'] / 1000:.0f}s")
            return [", ".join(parts)]
        return []

    def finish(self) -> Result | None:
        if self.result is None and self.last:
            self.result = Result(text=self.last.strip(), turns=self.turns, tokens=self.tokens)
        return super().finish()


PARSERS = {"claude": ClaudeParser, "gemini": GeminiParser, "antigravity": AntigravityParser,
           "copilot": CopilotParser}


def parser_for(agent: str) -> Parser:
    return PARSERS.get(agent, ClaudeParser)()
