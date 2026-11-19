"""Force delete (the tick box under the gear): how hard removing a crate's worktree tries."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory import janitor


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "CLEANUP_FILE", tmp_path / "cleanup.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "worktrees" / "crate-7"
    _git(repo, "worktree", "add", "-q", "-b", "crate/7", str(wt))
    (wt / "dirty.txt").write_text("uncommitted")
    _git(repo, "worktree", "lock", str(wt))
    return repo, wt


def _remove(repo, wt):
    for args in janitor.remove_args(wt, janitor.force_delete_on()):
        subprocess.run(["git", *args], cwd=repo, capture_output=True)


def test_off_by_default_and_saved(setup):
    assert janitor.force_delete_on() is False
    janitor.set_force_delete(True)
    assert janitor.force_delete_on() is True
    janitor.set_force_delete(False)
    assert janitor.force_delete_on() is False


def test_locked_worktree_needs_force(setup):
    repo, wt = setup
    _remove(repo, wt)
    assert wt.exists()  # off: git won't remove a locked worktree
    janitor.set_force_delete(True)
    _remove(repo, wt)
    assert not wt.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="finds programs through /proc here")
def test_stubborn_stops_programs_inside(tmp_path):
    folder = tmp_path / "crate-9"
    (folder / "node_modules").mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "60"], cwd=folder)
    try:
        assert janitor._processes_in(folder) == [proc.pid]
        assert janitor._processes_in(tmp_path / "crate-") == []  # not a prefix match on crate-9
        janitor.force_remove(folder, stubborn=True)
        assert not folder.exists()
        proc.wait(timeout=5)
    finally:
        proc.kill()
