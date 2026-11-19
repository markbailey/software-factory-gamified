"""Branch naming: the pattern machines name a crate's branch by (under the gear, or a line's own)."""
import asyncio
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from factory import branch_names, graph, janitor
from factory.config import RepoConfig

from .conftest import item, simple_layout


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.parametrize("raw,clean", [
    ("feature/12-add dark mode", "feature/12-add-dark-mode"),
    ("a..b", "a.b"),
    ("/x//y/", "x/y"),
    (".hidden/x.lock", "hidden/x"),
    ("we~ird^na:me?*[x]\\", "we-ird-na-me-x]"),
    ("x@{1}", "x@-1}"),
    ("@", ""),
    ("...", ""),
])
def test_sanitize_makes_legal_git_names(raw, clean, tmp_path):
    assert branch_names.sanitize(raw) == clean
    if clean:
        assert subprocess.run(["git", "check-ref-format", "--branch", clean], capture_output=True).returncode == 0


def test_validate():
    assert branch_names.validate("") is None
    assert branch_names.validate("feature/{issue}-{slug}") is None
    assert "isn't a placeholder" in branch_names.validate("feature/{ticket}")
    assert "stray" in branch_names.validate("feature/{issue")
    assert "empty" in branch_names.validate("/..")  # nothing but a slash and dots
    assert graph.validate_repo({"branch_pattern": "x/{nope}"}).startswith("branch naming:")
    assert graph.repo_settings({"repo": {"branch_pattern": " f/{id} "}}) == {"branch_pattern": "f/{id}"}


def test_values_and_render():
    it = {"id": 5, "source": "jira", "source_ref": "PROJ-12", "text": "Add Dark Mode!\nmore"}
    v = branch_names.values_for(it, "Build line", "coder", "owner/my-app", now=time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1)))
    assert branch_names.render("feature/{issue}-{slug}", v) == "feature/PROJ-12-add-dark-mode"
    assert branch_names.render("{line}/{machine}/{issue_number}-{date}", v) == "build-line/coder/12-2026-11-18"
    gh = branch_names.values_for({"id": 9, "source": "github", "source_ref": "34", "text": "x"})
    assert gh["issue"] == "34" and gh["issue_number"] == "34"
    typed = branch_names.values_for({"id": 9, "source": "game", "text": "x"})
    assert typed["issue"] == "9" and typed["issue_number"] == "9"


def test_choose_keeps_names_unique():
    v = {"id": "7", "slug": "fix-login"}
    assert branch_names.choose("feature/{slug}", v, set()) == "feature/fix-login"
    assert branch_names.choose("feature/{slug}", v, {"feature/fix-login"}) == "feature/fix-login-7"
    assert branch_names.choose("feature", v, {"feature/other"}) == "feature-7"  # git can't have both
    assert branch_names.choose("{slug}", {"id": "7", "slug": ""}, set()) == "crate/7"  # empty
    assert branch_names.choose("main", {"id": "7"}, set(), {"main"}) == "main-7"  # never the base branch
    assert branch_names.choose("crate/{issue}", {"id": "7", "issue": "3"}, set()) == "crate/3-7"  # not crate 3's


def test_factory_setting_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(branch_names, "SETTINGS_FILE", tmp_path / "branch_naming.json")
    assert branch_names.factory_pattern() == "crate/{id}"
    branch_names.set_factory_pattern("feature/{issue}")
    assert branch_names.factory_pattern() == "feature/{issue}"
    branch_names.set_factory_pattern("")
    assert branch_names.factory_pattern() == "crate/{id}"


def test_janitor_and_dashboard_know_custom_named_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "CLEANUP_FILE", tmp_path / "cleanup.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wts = tmp_path / "worktrees"
    _git(repo, "worktree", "add", "-q", "-b", "feature/PROJ-1-done", str(wts / "crate-1"))
    _git(repo, "worktree", "add", "-q", "-b", "feature/PROJ-2-busy", str(wts / "crate-2"))
    _git(repo, "branch", "someone-elses")
    old = time.time() - 3 * 86400
    items = {1: {"id": 1, "state": "shipped", "branch": "feature/PROJ-1-done", "pr_state": "MERGED",
                 "created_at": old, "finished_at": old},
             2: {"id": 2, "state": "running", "branch": "feature/PROJ-2-busy", "created_at": time.time()}}
    cfg = SimpleNamespace(worktrees=wts, default_repo=None, workspace=tmp_path)
    repos = [RepoConfig(github="o/r", path=repo, base_branch="main", remote="")]
    rows = {r["name"]: r for r in janitor.inventory(cfg, items, repos)["branches"]}
    assert rows["feature/PROJ-1-done"]["crate"] == 1 and rows["feature/PROJ-1-done"]["stale"]
    assert rows["feature/PROJ-2-busy"]["crate"] == 2 and rows["feature/PROJ-2-busy"]["in_use"]
    assert rows["someone-elses"]["crate"] is None

    removed = janitor.sweep(cfg, items, keep_scrapped_hours=24, repos=repos)
    left = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "feature/PROJ-1-done" not in left and any("feature/PROJ-1-done" in r for r in removed)
    assert "feature/PROJ-2-busy" in left and "someone-elses" in left


def _run(factory, text="Add a greeting"):
    async def go():
        async with factory.client() as c:
            n = await c.submit(text)
            await c.until_state(n, "shipped", "held", "scrapped")
            return n
    return asyncio.run(go())


def test_a_crate_ships_on_a_branch_named_by_the_pattern(factory):
    (factory.backend / "branch_naming.json").write_text(json.dumps({"pattern": "feature/{id}-{slug}"}))
    factory.start()
    n = _run(factory)
    crate = item(factory, n)
    assert crate["state"] == "shipped", crate["log"][-12:]
    assert crate["branch"] == f"feature/{n}-add-a-greeting"
    gh = (factory.state / "gh.log").read_text()
    assert f"feature/{n}-add-a-greeting" in gh or any(f"feature/{n}-add-a-greeting" in line for line in crate["log"])


def test_a_lines_own_pattern_wins(factory):
    (factory.backend / "branch_naming.json").write_text(json.dumps({"pattern": "feature/{id}"}))
    layout = simple_layout()
    layout["repo"] = {**layout.get("repo", {}), "branch_pattern": "{line}/{machine}/{id}"}
    factory.write_layout([layout])
    factory.start()
    n = _run(factory)
    crate = item(factory, n)
    assert crate["state"] == "shipped", crate["log"][-12:]
    assert crate["branch"] == f"build/coder/{n}"


def test_default_is_unchanged(factory):
    factory.start()
    n = _run(factory)
    assert item(factory, n)["branch"] == f"crate/{n}"
