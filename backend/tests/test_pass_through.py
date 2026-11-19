"""Machines switched off pass crates straight through; the Tester skips crates with no tests;
a Coder in Test writer only mode must be belted straight into a Tester."""
import asyncio
import copy

from factory import config, graph

from .conftest import item, simple_layout
from .test_scripts import git, repo, script

CATALOG, _, _ = config.load_catalog()


def problem(layout):
    fatal, problems = graph.check(CATALOG, [layout])
    assert fatal is None
    return problems.get(layout["id"])


def machine(layout, mid):
    return next(m for m in layout["machines"] if m["id"] == mid)


# The Tester skipping crates that add no tests ---------------------------------------------

def test_tester_skips_a_branch_that_changes_no_tests(tmp_path):
    r = repo(tmp_path)
    (r / "test_a.py").write_text("def test_fails():\n    assert False\n")  # would fail if run
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "a failing test on main")
    git(r, "checkout", "-q", "-b", "crate-1")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "code only")
    (r / "b.py").write_text("y = 2\n")  # not committed yet: still not a test
    out = script("run_tests.py", r, "--skip-without-test-changes", "main")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Skipping the tests" in out.stdout


def test_tester_runs_the_tests_when_the_branch_changes_one(tmp_path):
    r = repo(tmp_path)
    git(r, "checkout", "-q", "-b", "crate-1")
    (r / "tests").mkdir()
    (r / "tests" / "test_new.py").write_text("def test_fails():\n    assert False\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "a test")
    out = script("run_tests.py", r, "--skip-without-test-changes", "main")
    assert out.returncode != 0 and "This crate changes tests" in out.stdout


def test_tester_runs_the_tests_when_git_cant_tell(tmp_path):
    r = repo(tmp_path)
    (r / "test_a.py").write_text("def test_fails():\n    assert False\n")
    out = script("run_tests.py", r, "--skip-without-test-changes", "no-such-branch")
    assert out.returncode != 0 and "Couldn't tell" in out.stdout


def test_tester_skip_is_on_by_default_and_can_be_unticked():
    from factory.engine import _skip_reason

    def run_steps(options=None):
        entry = {"type": "tester", "id": "tester", **({"options": options} if options else {})}
        m = config.build_line(CATALOG, [entry])[0]
        return [s.command for s in m.steps if not _skip_reason(s.when, m.options, {})]
    on = run_steps()
    assert len(on) == 2 and "--skip-without-test-changes" in on[0]
    off = run_steps({"skip_without_tests": False})
    assert len(off) == 2 and "--skip-without-test-changes" not in off[0]


# A Test writer only Coder belted straight into a Tester ----------------------------------

def test_test_writer_coder_must_run_straight_into_a_tester():
    layout = simple_layout(["coder", "tester", "packager"])
    machine(layout, "coder")["options"] = {"coder_mode": "test_only"}
    assert problem(layout) is None

    between = simple_layout(["coder", "reviewer", "tester", "packager"])
    machine(between, "coder")["options"] = {"coder_mode": "test_only"}
    machine(between, "coder")["name"] = "Test bot"
    why = problem(between)
    assert why and "Test bot" in why and "Test writer only" in why and "Tester" in why

    off = copy.deepcopy(layout)
    machine(off, "tester")["enabled"] = False
    assert "switched off" in problem(off)


def test_other_coder_modes_need_no_tester():
    layout = simple_layout(["coder", "packager"])
    assert problem(layout) is None
    machine(layout, "coder")["options"] = {"coder_mode": "tdd"}
    assert problem(layout) is None


# Switching machines off --------------------------------------------------------------------

def test_only_some_types_can_be_switched_off():
    can = {t for t, m in CATALOG.items() if m.can_switch_off}
    assert {"verifier", "tester", "reviewer", "risk_analyser"} <= can
    assert not can & {"coder", "packager", "approver", "merger", "planner"}
    tester, coder_ = config.build_line(CATALOG, [{"type": "tester", "id": "t", "enabled": False},
                                                 {"type": "coder", "id": "c", "enabled": False}])
    assert tester.switched_off and not coder_.switched_off
    assert CATALOG["tester"].summary()["can_switch_off"] is True


def test_a_switched_off_risk_analyser_gives_no_risk():
    layout = simple_layout(["coder", "packager", "risk_analyser", "approver"])
    machine(layout, "approver")["auto_approve"] = "low"
    assert problem(layout) is None
    machine(layout, "risk_analyser")["enabled"] = False
    assert "needs a Risk analyser" in problem(layout)


def test_enabled_false_is_saved_and_reaches_the_line():
    from factory.engine import _clean_pipeline
    layout = simple_layout()
    machine(layout, "tester")["enabled"] = False
    clean = _clean_pipeline(layout)
    assert machine(clean, "tester")["enabled"] is False and "enabled" not in machine(clean, "coder")
    entries = graph.to_line(clean)
    assert next(e for e in entries if e["id"] == "tester")["enabled"] is False


def test_switched_off_tester_passes_crates_straight_through(make_factory):
    # Switched off it needs nothing (no Coder's branch), so a line of just the Tester ships.
    layout = simple_layout(["tester"])
    machine(layout, "tester")["enabled"] = False
    f = make_factory(layout=[layout])
    f.start()

    async def go():
        async with f.client() as c:
            n = await c.submit("Skip the tester")
            await c.until_state(n, "shipped", "held", "scrapped")
            return n, list(c.events)
    n, events = asyncio.run(go())
    crate = item(f, n)
    assert crate["state"] == "shipped", "\n".join(map(str, crate["log"][-14:]))
    assert any("Tester is switched off; passing straight through" in str(e) for e in crate["log"])
    assert not any(e.get("type") == "job_started" and e.get("machine") == "tester" for e in events)
