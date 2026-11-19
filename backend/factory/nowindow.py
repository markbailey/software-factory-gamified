"""On Windows, start every child process (git, gh, claude, our scripts) without a console window.

Without this, a tool can open a console window that flashes up on the desktop, e.g. when
the backend runs without a console of its own or a tool starts its own. CREATE_NO_WINDOW gives the
child a hidden console instead, which whatever it starts shares, so nothing flashes; output is
still piped back and logged as before. A caller that asks for a new or no console keeps it.
"""
from __future__ import annotations

import os
import subprocess

_applied = False


def apply() -> None:
    global _applied
    if os.name != "nt" or _applied:
        return
    _applied = True
    original = subprocess.Popen.__init__
    wants_console = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS

    def __init__(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & wants_console:
            kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
        if kwargs.get("startupinfo") is None:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = info
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__  # also covers subprocess.run and asyncio's subprocesses
