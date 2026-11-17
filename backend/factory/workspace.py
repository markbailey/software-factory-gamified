"""Makes sure each pipeline's repo exists as a local git clone, connected to its GitHub remote.

The default repo (factory.toml's [workspace], the throwaway test project) is created and given
a first commit if it's empty. Any other repo is only cloned and kept up to date: the factory
never pushes to its base branch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import Config, RepoConfig, github_name

IDENTITY = ["-c", "user.name=Software Factory", "-c", "user.email=factory@localhost"]

README = """# Test project

A throwaway project that the Software Factory works on.
Each crate gets its own branch (crate/<n>) and a pull request.
"""


GIT_SECONDS = 90


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is not installed or not on PATH")
    # Never wait on a sign-in prompt nobody can see: fail instead, and say so.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=GIT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} took over {GIT_SECONDS}s (is it waiting for a sign-in?)")
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


def _default_branch(repo: Path) -> str:
    """The branch origin's HEAD points at (the repo's default branch on GitHub), else main."""
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).stdout.strip()
    if not head:
        _git(repo, "remote", "set-head", "origin", "--auto", check=False)
        head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).stdout.strip()
    return head.split("/", 1)[1] if head.startswith("origin/") else "main"


def ensure_repo(cfg: Config, target: RepoConfig | None = None) -> list[str]:
    """Create or connect a pipeline's repo (the default one if none is given), filling in its
    base branch and remote if they weren't set. Returns lines describing what was done."""
    target = target or cfg.default_repo
    notes: list[str] = []
    repo, remote = target.path, target.remote
    # Only the test project, on its usual branch, gets a first commit pushed for it.
    scaffold = target.is_default and target.base_branch == cfg.base_branch
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.worktrees.mkdir(parents=True, exist_ok=True)

    if not (repo / ".git").exists():
        if remote and (not repo.exists() or not any(repo.iterdir())):
            repo.parent.mkdir(parents=True, exist_ok=True)
            _git(repo.parent, "clone", "-q", remote, str(repo))
            notes.append(f"cloned {remote} into {repo}")
        elif scaffold:
            repo.mkdir(parents=True, exist_ok=True)
            _git(repo, "init", "-q")
            notes.append(f"created git repo at {repo}")
        elif repo.exists() and any(repo.iterdir()):
            raise RuntimeError(f"{repo} isn't a git repo (and isn't empty, so it can't be cloned into)")
        else:
            raise RuntimeError(f"{repo} doesn't exist and there's no GitHub repo to clone into it")

    if not remote:  # a folder of yours: use its own origin, if it has one
        origin = _git(repo, "remote", "get-url", "origin", check=False)
        if origin.returncode == 0 and origin.stdout.strip():
            remote = target.remote = origin.stdout.strip()
            target.github = target.github or github_name(remote)
    if remote:
        _git(repo, "fetch", "-q", "origin")
    if not target.base_branch:
        target.base_branch = _default_branch(repo) if remote else (
            _git(repo, "branch", "--show-current", check=False).stdout.strip() or "main")
    base = target.base_branch
    if not scaffold:
        return notes + _follow(repo, base, remote)

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


def _follow(repo: Path, base: str, remote: str) -> list[str]:
    """A real repo: check its base branch exists and fast-forward it to GitHub's. Never pushes."""
    notes = []
    if remote:
        if not _ok(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}"):
            raise RuntimeError(f"{remote} has no branch {base}")
        if not _ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}"):
            _git(repo, "branch", "-q", "--track", base, f"origin/{base}", check=False)
        elif _ok(repo, "merge-base", "--is-ancestor", base, f"origin/{base}"):
            if _git(repo, "branch", "--show-current", check=False).stdout.strip() == base:
                _git(repo, "merge", "-q", "--ff-only", f"origin/{base}", check=False)
            else:
                _git(repo, "branch", "-q", "-f", base, f"origin/{base}", check=False)
        else:
            notes.append(f"note: local {base} in {repo} differs from origin/{base}; new branches start "
                         f"from origin/{base}")
    elif not _ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}"):
        raise RuntimeError(f"{repo} has no branch {base}")
    _git(repo, "worktree", "prune", check=False)
    return notes


def check_repo(cfg: Config, target: RepoConfig) -> dict:
    """What the game's Check button shows: does the repo exist on GitHub (and can you push to
    it), and is its folder usable. {"ok": bool, "message": str, ...}."""
    found: dict = {"ok": True, "github": target.github, "path": str(target.path),
                   "base_branch": target.base_branch, "default_branch": "", "lines": []}
    lines = found["lines"]
    if target.github:
        gh = shutil.which("gh")
        if not gh:
            return {**found, "ok": False, "message": "gh is not installed or not on PATH"}
        r = subprocess.run([gh, "repo", "view", target.github, "--json",
                            "nameWithOwner,defaultBranchRef,viewerPermission,url,isArchived"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip().splitlines()
            return {**found, "ok": False, "message": f"Can't find {target.github} on GitHub: "
                                                     f"{why[-1] if why else 'gh failed'}"}

        info = json.loads(r.stdout or "{}")
        found["github"] = info.get("nameWithOwner") or target.github
        found["url"] = info.get("url", "")
        found["default_branch"] = (info.get("defaultBranchRef") or {}).get("name", "")
        permission = str(info.get("viewerPermission") or "")
        lines.append(f"Found {found['github']} on GitHub (default branch {found['default_branch'] or '?'}).")
        if info.get("isArchived"):
            found["ok"] = False
            lines.append("It's archived, so nothing can be pushed to it.")
        if permission not in ("ADMIN", "MAINTAIN", "WRITE"):
            found["ok"] = False
            lines.append(f"You can't push to it (your access: {permission.lower() or 'none'}), so the "
                         "factory can't open pull requests there.")
        if target.base_branch and found["default_branch"] and target.base_branch != found["default_branch"]:
            b = subprocess.run([gh, "api", f"repos/{found['github']}/branches/{target.base_branch}", "--jq", ".name"],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            if b.returncode != 0:
                found["ok"] = False
                lines.append(f"It has no branch {target.base_branch}.")
    path = target.path
    if (path / ".git").exists():
        origin = _git(path, "remote", "get-url", "origin", check=False).stdout.strip()
        if target.github and github_name(origin).lower() != target.github.lower():
            found["ok"] = False
            lines.append(f"{path} is a clone of {origin or 'nothing'}, not {target.github}.")
        else:
            lines.append(f"Uses the clone in {path}.")
    elif path.exists() and any(path.iterdir()):
        found["ok"] = False
        lines.append(f"{path} has files in it but isn't a git repo.")
    elif target.github:
        lines.append(f"Will be cloned into {path} when you save.")
    else:
        found["ok"] = False
        lines.append(f"{path} doesn't exist. Give a GitHub repo to clone, or a folder that's a clone.")
    found["message"] = " ".join(lines)
    return found
