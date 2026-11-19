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


def test_check_changes_tests_only(tmp_path):
    """The Coder writing tests (TDD's first half, or Test writer only) may only touch tests."""
    r = repo(tmp_path)
    (r / "tests").mkdir()
    (r / "tests" / "test_a.py").write_text("def test_a(): assert False\n")
    (r / "src.test.ts").write_text("it('x', () => {})\n")
    git(r, "add", "-A")
    assert script("check_changes.py", r, "tests").returncode == 0
    assert script("check_changes.py", r, "test_only").returncode == 0
    (r / "a.txt").write_text("code too\n")
    git(r, "add", "-A")
    out = script("check_changes.py", r, "test_only")
    assert out.returncode != 0 and "a.txt" in out.stdout
    # TDD's second half may change anything; Implement may not touch tests.
    assert script("check_changes.py", r, "tdd").returncode == 0
    out = script("check_changes.py", r, "implement")
    assert out.returncode != 0 and "test_a.py" in out.stdout and "src.test.ts" in out.stdout


def test_check_changes_implement_writes_no_tests(tmp_path):
    """Implement mode writes code only: test setup like package.json is fine, tests are not."""
    r = repo(tmp_path)
    (r / "a.py").write_text("x = 1\n")
    (r / "package.json").write_text("{}\n")
    (r / "tests" / "__pycache__").mkdir(parents=True)  # left by the Tester's run: not a test
    (r / "tests" / "__pycache__" / "test_a.cpython-313.pyc").write_bytes(b"x")
    git(r, "add", "-A")
    assert script("check_changes.py", r, "implement").returncode == 0
    (r / "test_a.py").write_text("def test_a(): pass\n")
    git(r, "add", "-A")
    out = script("check_changes.py", r, "implement")
    assert out.returncode != 0 and "test_a.py" in out.stdout


def test_check_tests_fail_wants_red(tmp_path):
    """TDD: the tests written before the code must fail; passing or missing tests fail the step."""
    r = repo(tmp_path)
    assert script("check_tests_fail.py", r).returncode != 0  # no tests at all
    (r / "test_new.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                   "    def test_it(self):\n        from feature import thing\n"
                                   "        self.assertEqual(thing(), 1)\n")
    out = script("check_tests_fail.py", r)
    assert out.returncode == 0, out.stdout + out.stderr
    (r / "feature.py").write_text("def thing():\n    return 1\n")
    out = script("check_tests_fail.py", r)
    assert out.returncode != 0 and "already pass" in out.stdout


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


PLAN_REPLY = '## Summary\nx\n```json\n{"title": "P", "issues": [{"key": "T1", "title": "One", "depends_on": []}]}\n```'


def write_document(tmp_path, kind, reply):
    return subprocess.run([sys.executable, str(SCRIPTS / "write_document.py"), "--kind", kind, "--repo", "o/r",
                           "--label", "factory", "--plans", str(tmp_path / "plans"), "--crate", "1"],
                          input=reply + "\n<<<NOTE>>>\n", cwd=tmp_path, capture_output=True, text=True)


def test_document_writer_set_to_a_kind_only_writes_that_kind(tmp_path):
    """Set to review, a plan isn't turned into issues; set to a PR description, a crate with no
    code fails; set to issues, a review isn't posted. Each leaves by the red port saying why."""
    wrong = write_document(tmp_path, "review", PLAN_REPLY)
    assert wrong.returncode == 1 and "set to write a review" in wrong.stdout
    pr = write_document(tmp_path, "pr", PLAN_REPLY)
    assert pr.returncode == 1 and "no code of its own" in pr.stdout
    issues = write_document(tmp_path, "issues", "Verdict: Approve\nfine")
    assert issues.returncode == 1 and "set to write issues from the plan" in issues.stdout
    assert not (tmp_path / "plans").exists()


def test_step_when_a_choice_has_a_value():
    from factory.engine import _skip_reason
    opts = {"doc_kind": "auto"}
    assert _skip_reason("pr_draft,doc_kind=auto|pr", opts, {"pr_draft": "yes"}) == ""
    assert _skip_reason("doc_kind=auto|pr", {"doc_kind": "issues"}, {}) != ""
    assert _skip_reason("!doc_kind=pr", {"doc_kind": "issues"}, {}) == ""
    assert _skip_reason("!doc_kind=pr", {"doc_kind": "pr"}, {}) != ""


def test_default_lines_set_their_document_writers():
    assert graph.RENAMED_TYPES["issue_maker"] == ("doc_writer", {"doc_kind": "issues"})
    assert graph.RENAMED_TYPES["pr_poster"] == ("doc_writer", {"doc_kind": "review"})


def test_document_writer_makes_jira_issues_from_a_plan(tmp_path):
    """Set to Jira, a plan becomes one Jira issue per task in the project you set: ready ones
    get the label, the rest an "is blocked by" link; run again, nothing is made twice. On Auto a
    plan still goes to GitHub."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    calls = []

    class Jira(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, self.headers["Authorization"]))
            out = {"key": f"SF-{len(calls)}"} if self.path == "/rest/api/3/issue" else {}
            raw = json.dumps(out).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Jira)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    plan = ('## Summary\nx\n```json\n{"title": "P", "issues": [{"key": "T1", "title": "One", "body": "Do `a`\\n\\n'
            '### Acceptance criteria\\n- [ ] works", "depends_on": []}, {"key": "T2", "title": "Two", "depends_on": ["T1"]}]}\n```')
    site = f"http://127.0.0.1:{server.server_port}"

    def run(kind, **extra):
        argv = [sys.executable, str(SCRIPTS / "write_document.py"), "--kind", kind, "--repo", "", "--label", "factory",
                "--plans", str(tmp_path / "plans"), "--crate", "7", "--jira-url", site, "--jira-email", "me@x",
                "--jira-label", "factory", "--jira-type", "Story"]
        for k, v in extra.items():
            argv += ["--" + k.replace("_", "-"), v]
        import os
        return subprocess.run(argv, input=plan + "\n<<<NOTE>>>\n", cwd=tmp_path, capture_output=True, text=True,
                              env={**os.environ, "FACTORY_JIRA_TOKEN": "tok"})

    try:
        no_project = run("jira")
        assert no_project.returncode == 1 and "Jira project key" in no_project.stdout
        made = run("jira", jira_project="sf")
        assert made.returncode == 0, made.stdout + made.stderr
        issues = [c for c in calls if c[0] == "/rest/api/3/issue"]
        assert len(issues) == 2
        first, second = issues[0][1]["fields"], issues[1][1]["fields"]
        assert first["project"] == {"key": "SF"} and first["issuetype"] == {"name": "Story"}
        assert first["labels"] == ["factory"] and "labels" not in second
        assert first["description"]["type"] == "doc"
        link = next(c[1] for c in calls if c[0] == "/rest/api/3/issueLink")
        assert link["inwardIssue"] == {"key": "SF-2"} and link["outwardIssue"] == {"key": "SF-1"}
        record = json.loads((tmp_path / "plans" / "crate-7.json").read_text())
        assert record["repo"] == "jira:SF" and record["tasks"]["T1"]["number"] == "SF-1"
        before = len(calls)
        assert run("jira", jira_project="SF").returncode == 0 and len(calls) == before  # not made twice
    finally:
        server.shutdown()

    from factory import plans
    assert plans.shipped(tmp_path / "plans", "jira:SF", "SF-1") == ["SF-2"]
    # On Auto a plan is still GitHub issues (here: no repo set, so it says so).
    auto = run("auto")
    assert auto.returncode == 1 and "No repo to create issues in" in auto.stdout
