"""The machines' prompts for Claude, kept as Markdown files under backend/prompts/<machine>/.

machines.toml points at them: `prompt_file = "coder/prompt.md"` on a machine type is its default
prompt (what fills {prompt}, and what the game shows in its settings), and `stdin_file =
"coder/write_tests.md"` on a step is what that step sends Claude. The {placeholders} in a file are
filled as they always were.

A file is read again every time a crate uses it, so editing one takes effect on the next run
without a restart. A machine whose prompt you edited in the game keeps your text; one left on the
default follows its file. A file that goes missing or empty while the backend runs falls back to
the text it had when the backend started (it must be there at start). The Reviewer's review types
are in prompts/reviewer/<type>.md too (see review_types.py).
"""
from pathlib import Path

from . import paths

PROMPTS_DIR = paths.BACKEND_DIR / "prompts"

_warned: set[str] = set()


def load(rel: str) -> str:
    """A prompt file's text, for loading machines.toml. Raises ValueError if it is missing or empty."""
    text = _read(rel)
    if not text.strip():
        raise ValueError(f"prompt file {PROMPTS_DIR / rel} is missing or empty")
    return text


def read(rel: str, fallback: str) -> str:
    """A prompt file's text as it is now; fallback (its text at start) if it can't be read."""
    text = _read(rel)
    if text.strip():
        _warned.discard(rel)
        return text
    if rel not in _warned:
        _warned.add(rel)
        print(f"[factory] prompt file {PROMPTS_DIR / rel} is missing or empty; using the text it had at start")
    return fallback


def same(a: str | None, b: str | None) -> bool:
    """Whether two prompts are the same text, ignoring spacing and line breaks."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _read(rel: str) -> str:
    try:
        return (PROMPTS_DIR / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
