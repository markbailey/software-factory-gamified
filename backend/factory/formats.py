"""Turns raw tool output into readable log lines for the game."""
from __future__ import annotations

import json

TOOL_DETAIL_KEYS = ("file_path", "path", "pattern", "command", "url")


def claude_stream_json(line: str) -> list[str]:
    """Summarise one line of `claude -p --output-format stream-json --verbose` output.

    Returns zero or more log lines. Lines that are not JSON pass through unchanged.
    """
    try:
        msg = json.loads(line)
    except ValueError:
        return [line] if line.strip() else []
    if not isinstance(msg, dict):
        return []

    kind = msg.get("type")
    if kind == "system" and msg.get("subtype") == "init":
        return [f"[claude] session started (model {msg.get('model', 'unknown')})"]

    if kind == "assistant":
        out: list[str] = []
        for block in msg.get("message", {}).get("content", []) or []:
            if block.get("type") == "text":
                out.extend(t for t in block.get("text", "").splitlines() if t.strip())
            elif block.get("type") == "tool_use":
                args = block.get("input", {}) or {}
                detail = next((str(args[k]) for k in TOOL_DETAIL_KEYS if k in args), "")
                out.append(f"  > {block.get('name', 'tool')} {detail}".rstrip())
        return out

    if kind == "result":
        parts = [f"[claude] {msg.get('subtype', 'finished')}"]
        if "num_turns" in msg:
            parts.append(f"{msg['num_turns']} turns")
        if msg.get("total_cost_usd") is not None:
            parts.append(f"${msg['total_cost_usd']:.4f}")
        if msg.get("duration_ms") is not None:
            parts.append(f"{msg['duration_ms'] / 1000:.0f}s")
        lines = [", ".join(parts)]
        if msg.get("is_error"):
            lines.append(f"[claude] error: {msg.get('result', '')}")
        return lines

    return []  # stream deltas, status pings and the like


FORMATTERS = {
    "text": lambda line: [line],
    "claude-stream-json": claude_stream_json,
}
