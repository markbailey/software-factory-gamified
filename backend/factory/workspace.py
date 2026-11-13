"""Makes sure the test project exists as a git repo, connected to its GitHub remote."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import Config

IDENTITY = ["-c", "user.name=Software Factory", "-c", "user.email=factory@localhost"]

README = """# Test project

A throwaway project that the Software Factory works on.
Each crate gets its own branch (crate/<n>) and a pull request.
"""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is not installed or not on PATH")
    result = subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def _ok(cwd: Path, *args: str) -> bool:
    return _git(cwd, *args, check=False).returncode == 0


def _is_factory_scaffold(repo: Path, branch: str) -> bool:
    """True if the branch is only the single first commit this backend made itself."""
    log = _git(repo, "log", "--format=%an|%s", branch, check=False).stdout.strip().splitlines()
    return log == ["Software Factory|Initial commit"]


def _first_commit(repo: Path, base: str) -> None:
    _git(repo, "checkout", "-q", "-B", base)
    readme = repo / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *IDENTITY, "commit", "-q", "-m", "Initial commit")


def ensure_repo(cfg: Config) -> list[str]:
    """Create or connect the test project repo. Returns lines describing what was done."""
    notes: list[str] = []
    repo, base, remote = cfg.repo, cfg.base_branch, cfg.remote
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.worktrees.mkdir(parents=True, exist_ok=True)

    if not (repo / ".git").exists():
        if remote and (not repo.exists() or not any(repo.iterdir())):
            _git(cfg.workspace, "clone", "-q", remote, str(repo))
            notes.append(f"cloned {remote}")
        else:
            repo.mkdir(parents=True, exist_ok=True)
            _git(repo, "init", "-q")
            notes.append(f"created git repo at {repo}")

    local = f"refs/heads/{base}"
    if remote:
        current = _git(repo, "remote", "get-url", "origin", check=False)
        if current.returncode != 0:
            _git(repo, "remote", "add", "origin", remote)
            notes.append(f"added remote origin {remote}")
        elif current.stdout.strip() != remote:
            notes.append(f"warning: origin is {current.stdout.strip()}, not {remote}; leaving it")
        _git(repo, "fetch", "-q", "origin")

    has_local = _ok(repo, "rev-parse", "--verify", "--quiet", local)
    has_remote = bool(remote) and _ok(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}")

    if not has_local and not has_remote:
        _first_commit(repo, base)
        notes.append(f"made the first commit on {base}")
        has_local = True

    if remote:
        if has_local and not has_remote:
            _git(repo, "push", "-q", "-u", "origin", base)
            notes.append(f"pushed {base} to origin")
        elif not has_local or _is_factory_scaffold(repo, base) and not _ok(
                repo, "merge-base", "--is-ancestor", f"origin/{base}", base):
            # Nothing of ours worth keeping: follow GitHub's main.
            _git(repo, "checkout", "-q", "-B", base, f"origin/{base}")
            notes.append(f"{base} now follows origin/{base}")
        elif _ok(repo, "merge-base", "--is-ancestor", base, f"origin/{base}"):
            _git(repo, "checkout", "-q", base)
            _git(repo, "merge", "-q", "--ff-only", f"origin/{base}")
        elif _ok(repo, "merge-base", "--is-ancestor", f"origin/{base}", base):
            _git(repo, "push", "-q", "origin", base)
            notes.append(f"pushed local {base} commits to origin")
        else:
            notes.append(f"warning: local {base} and origin/{base} have diverged; new branches start "
                         f"from origin/{base}. Fix by hand in {repo} if needed.")
        _git(repo, "branch", "-q", f"--set-upstream-to=origin/{base}", base, check=False)

    _git(repo, "worktree", "prune", check=False)
    return notes
