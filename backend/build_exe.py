"""Builds the backend into one exe with PyInstaller: dist/SoftwareFactoryServer(.exe).

Run from the backend folder:  .venv\\Scripts\\python build_exe.py   (or build_exe.bat)
Needs PyInstaller in that Python:  .venv\\Scripts\\python -m pip install pyinstaller

The exe carries the default prompts/, scripts/, machines.toml, factory.toml and
accounts.example.toml (factory/paths.py DEFAULTS). Put it in any folder and run it: it writes
whichever of those are missing beside itself, then reads and writes everything (accounts.toml,
factory.db, the game's saved layout) in that folder. Files already there are never overwritten.
Copy it into this backend folder to run it on your current settings and data.
"""
import os
import subprocess
import sys
from pathlib import Path

from factory.paths import DEFAULTS

HERE = Path(__file__).resolve().parent
NAME = "SoftwareFactoryServer"

# The standard library modules the machines' scripts use that the server itself doesn't import, so
# the exe can run the scripts when there is no Python on PATH.
SCRIPT_MODULES = ["argparse", "base64", "fnmatch", "importlib.util", "unittest", "urllib.request",
                  "urllib.error", "datetime"]


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(f"PyInstaller isn't installed in {sys.executable}. Install it with:\n"
              f"  \"{sys.executable}\" -m pip install pyinstaller")
        return 1
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", NAME, "--paths", str(HERE),
            "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"),
            "--specpath", str(HERE / "build"),
            # uvicorn and websockets pick their parts by name at run time
            "--collect-submodules", "uvicorn", "--collect-submodules", "websockets",
            # the scripts read and write UTF-8, as PYTHONUTF8 does when run from source
            "--python-option", "X utf8"]
    for name in DEFAULTS:
        src = HERE / name
        if src.exists():
            # a file's destination is the folder it goes in; a folder's is its own name
            args += ["--add-data", f"{src}{os.pathsep}{name if src.is_dir() else '.'}"]
    for mod in SCRIPT_MODULES:
        args += ["--hidden-import", mod]
    args.append(str(HERE / "server.py"))
    print(" ".join(f'"{a}"' if " " in a else a for a in args))
    code = subprocess.call(args, cwd=HERE)
    if code == 0:
        exe = HERE / "dist" / (NAME + (".exe" if os.name == "nt" else ""))
        print(f"\nBuilt {exe}")
    return code


if __name__ == "__main__":
    sys.exit(main())
