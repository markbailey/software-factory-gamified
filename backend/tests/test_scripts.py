"""Unit tests for the Coder's git scripts and the layout fixer."""
import subprocess
import sys
from pathlib import Path

from factory import graph

from .conftest import simple_layout

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
WHO = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd, *args):
    return subprocess.run(["git", *WHO, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def script(name, cwd, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=cwd, capture_output=True, text=True)


def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


def test_make_branch_new_then_reused(tmp_path):
    r = repo(tmp_path)
    wt = tmp_path / "wt"
    assert script("make_branch.py", r, "crate/1", str(wt), "main").returncode == 0
    (wt / "b.txt").write_text("work\n")
    again = script("make_branch.py", r, "crate/1", str(wt), "main")
    assert again.returncode == 0 and "Picking up" in again.stdout
    assert (wt / "b.txt").exists()


def test_make_branch_when_only_the_branch_is_left(tmp_path):
    """The worktree folder is gone but the branch exists: the old "already exists" failure."""
    r = repo(tmp_path)
    wt = tmp_path / "wt"
    script("make_branch.py", r, "crate/1", str(wt), "main")
    (wt / "b.txt").write_text("work\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "work")
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=r, check=True)
    out = script("make_branch.py", r, "crate/1", str(wt), "main")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "existing branch" in out.stdout and (wt / "b.txt").exists()


def test_merge_base_leaves_conflicts_for_the_coder(tmp_path):
    r = repo(tmp_path)
    wt = tmp_path / "wt"
    script("make_branch.py", r, "crate/1", str(wt), "main")
    (wt / "a.txt").write_text("crate's version\n")
    git(wt, "commit", "-q", "-am", "crate")
    (r / "a.txt").write_text("main's version\n")
    git(r, "commit", "-q", "-am", "main moved on")
    out = script("merge_base.py", wt, "main")
    assert out.returncode == 0 and "a.txt" in out.stdout
    assert "<<<<<<< " in (wt / "a.txt").read_text()
    # The Coder's commit refuses to go through with markers still in the files.
    git(wt, "add", "-A")
    assert script("check_changes.py", wt).returncode != 0
    (wt / "a.txt").write_text("both versions\n")
    git(wt, "add", "-A")
    assert script("check_changes.py", wt).returncode == 0


def test_merge_base_clean_merge_and_nothing_to_do(tmp_path):
    r = repo(tmp_path)
    wt = tmp_path / "wt"
    script("make_branch.py", r, "crate/1", str(wt), "main")
    assert "already has everything" in script("merge_base.py", wt, "main").stdout
    (r / "c.txt").write_text("new on main\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "main moved on")
    out = script("merge_base.py", wt, "main")
    assert out.returncode == 0 and "Merged" in out.stdout and (wt / "c.txt").exists()
    # Claude has nothing to add: the merge alone still counts as a change, and the commit finishes it.
    git(wt, "add", "-A")
    assert script("check_changes.py", wt).returncode == 0
    git(wt, "commit", "-q", "-m", "crate")
    assert len(git(wt, "log", "-1", "--format=%P").split()) == 2  # a merge commit


def test_rehome_moves_a_machine_to_the_line_that_reaches_it():
    """A packager placed while line A was picked, but only line B's belts run through it."""
    a = simple_layout(["coder", "packager"], pid="a", name="A")
    a["belts"] = [{"from": "intake", "port": "out", "to": "coder"}, {"from": "coder", "port": "pass", "to": "dock"},
                  {"from": "packager", "port": "pass", "to": "b:dock"}]
    b = simple_layout(["b_coder"], pid="b", name="B", row=10)
    b["machines"][0]["type"] = "coder"
    b["belts"] = [{"from": "intake", "port": "out", "to": "b_coder"}, {"from": "b_coder", "port": "pass", "to": "packager"}]
    assert graph.rehome([a, b]) == ["packager"]
    assert [m["id"] for m in a["machines"]] == ["coder"]
    assert "packager" in [m["id"] for m in b["machines"]]
    assert {"from": "packager", "port": "pass", "to": "dock"} in b["belts"]
