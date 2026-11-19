"""The Reviewer's review types: the drop-down, the prompt text and the material it gathers."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from factory import config, review_types

from .conftest import _shim
from factory.engine import Factory

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FAKES = Path(__file__).resolve().parent / "fakes"
WHO = ["-c", "user.name=t", "-c", "user.email=t@t"]


def reviewer():
    catalog, _, _ = config.load_catalog()
    return catalog["reviewer"]


def test_dropdown_matches_the_review_types():
    spec = next(s for s in reviewer().option_specs if s["id"] == "review_type")
    assert spec["choices"] == review_types.choices()
    assert spec["default"] == "general"
    shown = {s["id"]: s.get("show_when", "") for s in reviewer().option_specs}
    assert shown["rules_file"] == "review_type=security" and shown["doc_paths"] == "review_type=intent"
    assert shown["fail_on_extra_work"] == shown["min_confidence"] == "review_type=intent"
    assert shown["fail_on_changes"] == ""


def test_every_type_ends_in_the_verdict_and_fills_its_blanks():
    for kind in review_types.TYPES:
        text = review_types.focus({"review_type": kind})
        assert "Review type:" in text and "Ask for changes" in text
        assert "{" not in text, (kind, text)


def test_settings_change_the_focus():
    sec = review_types.focus({"review_type": "security", "rules_file": "docs/sec.md", "security_fail_level": "high"})
    assert "reference/ folder" in sec and "only for high severity" in sec
    assert "no security rules file" in review_types.focus({"review_type": "security"})
    assert "Out-of-date documents also count" in review_types.focus({"review_type": "intent", "fail_on_stale_docs": True})
    assert "does more than was asked" in review_types.focus({"review_type": "intent", "fail_on_extra_work": True})
    intent = review_types.focus({"review_type": "intent"})
    assert "Confidence it matches" in intent and "below 70%" in intent and "none were set" in intent
    assert "below 85%" in review_types.focus({"review_type": "intent", "min_confidence": "85"})
    assert "Low confidence alone is a comment" in review_types.focus({"review_type": "intent", "min_confidence": "off"})
    custom = review_types.focus({"review_type": "custom", "custom_checklist": "no {spec} here, no print()"})
    assert "no {spec} here, no print()" in custom
    assert review_types.focus({"review_type": "nonsense"}) == review_types.focus({})
    assert review_types.reference_files({"review_type": "intent", "doc_paths": " docs/*.md "}) == "docs/*.md"
    assert review_types.reference_files({"review_type": "tests", "doc_paths": "docs/"}) == ""


def test_old_docs_and_ticket_types_become_intent_with_their_settings():
    """Layouts saved with the two types that were merged still load, as the intent check."""
    for old, extra in (("docs", {"doc_paths": "docs/", "fail_on_stale_docs": True}),
                       ("ticket", {"fail_on_extra_work": True})):
        assert review_types.focus({"review_type": old, **extra}) == review_types.focus({"review_type": "intent", **extra})
    assert review_types.reference_files({"review_type": "docs", "doc_paths": "docs/"}) == "docs/"


def _values(tmp_path, machine):
    fake = SimpleNamespace(
        repo_of=lambda item: config.RepoConfig(github="o/r", path=tmp_path, base_branch="main", remote=""),
        cfg=SimpleNamespace(worktrees=tmp_path / "wt", workspace=tmp_path, scripts_dir=SCRIPTS,
                            github=SimpleNamespace(repo="o/r", label="factory")),
        account_choice=lambda item, kind: "")
    return Factory._values(fake, {"id": 7, "text": "Add a login page"}, machine)


def test_prompt_gets_the_chosen_focus(tmp_path):
    m = reviewer()
    m.options["review_type"] = "intent"
    values = _values(tmp_path, m)
    assert "Review type: intent check" in values["prompt"]
    assert "Add a login page" in values["prompt"] and "{review_focus}" not in values["prompt"]
    m.prompt = "An old saved prompt.\n{spec}"  # saved before review types: the focus is added on the end
    assert "Review type: intent check" in _values(tmp_path, m)["prompt"]


def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "docs" / "api").mkdir(parents=True)
    (r / "docs" / "arch.md").write_text("Use the service layer.\n")
    (r / "docs" / "api" / "users.md").write_text("GET /users\n")
    (r / "docs" / "api" / "logo.png").write_bytes(b"\x89PNG\0\0\0")
    (r / "src.py").write_text("print(1)\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "first"]):
        subprocess.run(["git", *WHO, *args], cwd=r, check=True, capture_output=True)
    (r / "docs" / "arch.md").write_text("Edited on a branch, not committed.\n")
    return r


def prepare(tmp_path, kind, files, pr_url="", *extra, env=None, stdin=""):
    out = tmp_path / "review"
    return out, subprocess.run([sys.executable, str(SCRIPTS / "prepare_review.py"), str(out), kind,
                                str(_repo(tmp_path)), "main", files, pr_url, *extra], capture_output=True,
                               text=True, env=env, input=stdin)


def test_prepare_copies_docs_from_the_base_branch(tmp_path):
    out, r = prepare(tmp_path, "intent", "docs/arch.md, ./docs/api/*, missing/*.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "reference" / "docs" / "arch.md").read_text() == "Use the service layer.\n"
    assert (out / "reference" / "docs" / "api" / "users.md").exists()
    assert not (out / "reference" / "docs" / "api" / "logo.png").exists()  # binary left out
    assert not (out / "reference" / "src.py").exists()
    readme = (out / "reference" / "README.md").read_text()
    assert "missing/*.md" in readme and "- docs/arch.md" in readme


def test_prepare_folder_pattern_and_nothing_set(tmp_path):
    out, r = prepare(tmp_path, "security", "docs/")
    assert r.returncode == 0 and (out / "reference" / "docs" / "api" / "users.md").exists()
    out2 = tmp_path / "other"
    r2 = subprocess.run([sys.executable, str(SCRIPTS / "prepare_review.py"), str(out2), "custom",
                         str(tmp_path / "repo"), "main", ""], capture_output=True, text=True)
    assert r2.returncode == 0 and "No files were set" in (out2 / "reference" / "README.md").read_text()


def test_prepare_general_does_nothing_and_a_game_crate_has_only_its_spec(tmp_path):
    out, r = prepare(tmp_path, "general", "")
    assert r.returncode == 0 and not (out / "reference").exists()
    out2 = tmp_path / "t"
    r2 = subprocess.run([sys.executable, str(SCRIPTS / "prepare_review.py"), str(out2), "ticket",
                         str(tmp_path / "repo"), "main", "", ""], capture_output=True, text=True)
    sources = (out2 / "sources.md").read_text()
    assert r2.returncode == 0 and not (out2 / "ticket.md").exists() and not (out2 / "plan.md").exists()
    assert "typed in the game" in sources and "no documentation was set" in sources


def test_intent_gathers_the_issue_the_plan_and_the_prd(tmp_path):
    """A crate from a planned issue: its issue, the approved plan (tracking issue), the PRD the plan
    came from, and the PR text a Document writer drafted, all for Claude to check against."""
    state, bin_dir, plans = tmp_path / "state", tmp_path / "bin", tmp_path / "plans"
    for d in (state, bin_dir, plans):
        d.mkdir()
    _shim(bin_dir, "gh", FAKES / "fake_gh.py")
    issues = [{"number": 5, "title": "PRD: dark mode", "body": "Users want a dark theme.", "url": "u5"},
              {"number": 6, "title": "Plan: dark mode", "url": "u6",
               "body": "Planned by the Software Factory from #5.\n\n## Tasks\n- [ ] #7"},
              {"number": 7, "title": "Add a theme toggle", "body": "A toggle in settings.", "url": "u7"}]
    (state / "issues.json").write_text(json.dumps([{**i, "state": "OPEN", "labels": []} for i in issues]))
    (plans / "crate-3.json").write_text(json.dumps({"repo": "o/r", "crate": 3, "title": "dark mode", "tracking": 6,
                                                    "tasks": {"T1": {"number": 7, "title": "Add a theme toggle"}}}))
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / "pr.md").write_text("# Changes on crate/9 (no pull request yet)\n")
    env = {**os.environ, "FAKE_STATE": str(state), "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}
    out, r = prepare(tmp_path, "intent", "docs/arch.md", "", "--source", "github", "--source-ref", "7",
                     "--github-repo", "o/r", "--plans", str(plans), "--draft-on-stdin",
                     env=env, stdin="Adds a dark theme toggle.")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "A toggle in settings." in (out / "ticket.md").read_text()
    plan = (out / "plan.md").read_text()
    assert "task T1" in plan and "Planned by the Software Factory" in plan and "Users want a dark theme." in plan
    assert "Adds a dark theme toggle." in (out / "pr.md").read_text()
    assert (out / "reference" / "docs" / "arch.md").exists()
    sources = (out / "sources.md").read_text()
    assert "approved plan (#6)" in sources and "PRD (#5)" in sources and "drafted PR description" in sources


def test_a_docs_review_reads_the_docs_and_ships(make_factory):
    """End to end: a documentation Reviewer copies the doc from the base branch and Claude gets the focus."""
    import asyncio

    from .conftest import item, simple_layout
    layout = simple_layout(["coder", "reviewer"])
    # Saved with the old documentation type: it loads as the intent check, keeping its docs.
    layout["machines"][1]["options"] = {"review_type": "docs", "doc_paths": "README.md", "comment_on_pr": False}
    f = make_factory(layout=[layout])
    f.start()

    async def go():
        async with f.client() as c:
            n = await c.submit("Document the thing")
            await c.until_state(n, "shipped", "held", "scrapped", timeout=90)
            return n
    n = asyncio.run(go())
    crate = item(f, n)
    lines = "\n".join(str(e) for e in crate["log"])
    assert crate["state"] == "shipped", lines[-3000:]
    assert "Copied 1 reference file(s)" in lines and "README.md" in lines
    review_dir = f.root / "workspace" / "reviews" / f"crate-{n}"
    assert (review_dir / "reference" / "README.md").exists()
    assert (review_dir / "sources.md").exists()  # gathered as an intent check


def test_prompt_text_comes_from_the_md_files_with_a_fallback(tmp_path):
    """Each type's text is read from prompts/reviewer/<type>.md; missing or empty falls back."""
    for kind, (_, built_in) in review_types.TYPES.items():  # the shipped files match the built-in text
        assert (review_types.PROMPTS_DIR / f"{kind}.md").read_text(encoding="utf-8").strip() == built_in.strip()
    (tmp_path / "tests.md").write_text("Review type: my tests.\nAsk for changes when sad.\n", encoding="utf-8")
    (tmp_path / "intent.md").write_text("Edited intent.{confidence_rule}\n", encoding="utf-8")
    (tmp_path / "general.md").write_text("  \n", encoding="utf-8")
    assert review_types.focus({"review_type": "tests"}, tmp_path) == "Review type: my tests.\nAsk for changes when sad."
    assert review_types.focus({"review_type": "intent", "min_confidence": "85"}, tmp_path) == \
        "Edited intent. Also ask for changes when your confidence is below 85%."
    assert review_types.focus({}, tmp_path) == review_types.TYPES["general"][1]  # empty file
    assert review_types.focus({"review_type": "security"}, tmp_path) == review_types.focus({"review_type": "security"})
