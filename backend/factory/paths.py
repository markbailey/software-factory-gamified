"""Where the backend's files are, run from source or as a PyInstaller exe.

From source, everything is in the backend folder (the one holding factory/). Built as an exe
(backend/build_exe.py), BACKEND_DIR is the folder the exe is in: factory.toml, machines.toml,
accounts.toml, factory.db, the game's saved layout and the prompts/ and scripts/ folders all live
beside the exe, so you can edit them and they survive a rebuild.

The exe carries a copy of the editable defaults (DEFAULTS below). Each time it starts it writes any
of those files that are missing beside it, and never touches one that is already there, so your
edits are kept. Delete a file to get the shipped version back on the next start.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# Where the shipped copies are: inside the exe's bundle when frozen, else the backend folder.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent.parent
# Where the backend reads and writes its files.
BACKEND_DIR = Path(sys.executable).resolve().parent if FROZEN else BUNDLE_DIR

# The files and folders the exe ships and unpacks beside itself when they are missing. The game's
# saved state (line.json, pipelines.json, repos.json, ...) and accounts.toml are not shipped: the
# backend starts without them and writes them when you change something in the game.
DEFAULTS = ("prompts", "scripts", "machines.toml", "factory.toml", "accounts.example.toml", "docs.toml")


def _shipped_files(bundle: Path) -> list[Path]:
    """Each shipped file, relative to bundle."""
    out: list[Path] = []
    for name in DEFAULTS:
        src = bundle / name
        if src.is_file():
            out.append(Path(name))
        elif src.is_dir():
            out += [p.relative_to(bundle) for p in sorted(src.rglob("*"))
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    return out


def unpack_defaults(bundle: Path | None = None, target: Path | None = None) -> list[Path]:
    """Copy each shipped file that is missing from target (BACKEND_DIR) out of bundle. Returns the
    files written, relative to target. Does nothing when run from source (they are the same folder)."""
    bundle = bundle or BUNDLE_DIR
    target = target or BACKEND_DIR
    if bundle.resolve() == target.resolve():
        return []
    written: list[Path] = []
    for rel in _shipped_files(bundle):
        dest = target / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / rel, dest)
        written.append(rel)
    return written


def python_command() -> str:
    """What {python} in machines.toml runs: this Python from source. The exe has no python.exe of
    its own, so it uses the Python on PATH if there is one (the scripts' tests run with it), and
    otherwise runs the scripts itself (see run_as_python)."""
    if not FROZEN:
        return sys.executable
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        # Skip the Microsoft Store stub, which only opens the Store.
        if found and "WindowsApps" not in found:
            return found
    return sys.executable


def run_as_python(argv: list[str]) -> bool:
    """Let the exe stand in for python.exe when it is asked to run a script: `exe script.py args`,
    `exe -m module args` or `exe -c code`. Returns False (did nothing) for any other command line.
    Only the standard library and what the backend itself bundles can be imported."""
    import runpy

    if len(argv) < 2:
        return False
    first = argv[1]
    if first.lower().endswith(".py"):
        script = str(Path(first).resolve())
        sys.argv = [script, *argv[2:]]
        sys.path.insert(0, str(Path(script).parent))
        runpy.run_path(script, run_name="__main__")
        return True
    if first == "-m" and len(argv) > 2:
        sys.argv = [argv[2], *argv[3:]]
        sys.path.insert(0, str(Path.cwd()))
        runpy.run_module(argv[2], run_name="__main__", alter_sys=True)
        return True
    if first == "-c" and len(argv) > 2:
        sys.argv = ["-c", *argv[3:]]
        exec(compile(argv[2], "<string>", "exec"), {"__name__": "__main__"})
        return True
    return False
