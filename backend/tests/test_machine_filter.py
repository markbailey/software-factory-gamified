"""A filter on a machine that can be switched off (the Verifier, Tester, Reviewer...): crates
its rules pick are worked on as usual; the rest pass straight out of its green port."""
import asyncio
import copy

from factory import config, graph
from factory.engine import _clean_pipeline

from .conftest import item, simple_layout

CATALOG, _, _ = config.load_catalog()


def problem(layout):
    fatal, problems = graph.check(CATALOG, [layout])
    assert fatal is None
    return problems.get(layout["id"])


def machine(layout, mid):
    return next(m for m in layout["machines"] if m["id"] == mid)


def test_filter_reaches_the_line_only_on_types_that_can_pass_through():
    f = {"match": "all", "rules": [{"when": "text", "value": " login "}, {"when": "", "value": "x"}]}
    verifier, coder = config.build_line(CATALOG, [{"type": "verifier", "id": "v", "filter": f},
                                                  {"type": "coder", "id": "c", "filter": f}])
    assert verifier.filter == {"match": "all", "rules": [{"when": "text", "value": "login"}]}
    assert coder.filter == {}
    assert verifier.summary()["can_filter"] and not coder.summary()["can_filter"]
    # No rules: it works on every crate, as before.
    assert config.build_line(CATALOG, [{"type": "verifier", "id": "v", "filter": {"rules": []}}])[0].filter == {}


def test_filter_is_saved_and_handed_to_the_line():
    layout = simple_layout()
    machine(layout, "tester")["filter"] = {"match": "any", "rules": [{"when": "source", "value": "game"}]}
    clean = _clean_pipeline(layout)
    assert machine(clean, "tester")["filter"]["rules"][0]["when"] == "source"
    entry = next(e for e in graph.to_line(clean) if e["id"] == "tester")
    assert entry["filter"]["rules"] == [{"when": "source", "value": "game"}]


def test_bad_filters_are_reported():
    layout = simple_layout()
    assert problem(layout) is None
    bad = copy.deepcopy(layout)
    machine(bad, "tester")["filter"] = {"rules": [{"when": "risk", "value": "huge"}]}
    assert "filter rule 1 needs a risk" in problem(bad)
    machine(bad, "tester")["filter"] = {"rules": [{"when": "otherwise", "value": ""}]}
    assert "doesn't understand" in problem(bad)
    machine(bad, "tester")["filter"] = {"match": "most", "rules": []}
    assert "any or all" in problem(bad)
    # Sorter rule checks still say which output.
    assert graph.rules_problem([{"port": "out_2", "when": "size", "value": "lots"}]) == \
        "the rule on output 2 needs a number of changed lines"


def test_filter_by_files_needs_a_branch_before_it():
    layout = simple_layout(["verifier", "coder", "packager"])
    machine(layout, "verifier")["filter"] = {"rules": [{"when": "files", "value": "docs/"}]}
    assert "filters by the files it changes" in problem(layout)
    machine(layout, "verifier")["filter"] = {"rules": [{"when": "text", "value": "docs"}]}
    assert problem(layout) is None
    machine(layout, "verifier")["enabled"] = False  # switched off, the filter doesn't matter
    machine(layout, "verifier")["filter"] = {"rules": [{"when": "files", "value": "docs/"}]}
    assert problem(layout) is None


def test_rule_matching_for_filters():
    from factory.engine import _rule_matches
    crate = {"text": "Fix the login page", "source": "github", "risk": "medium", "repo": "me/app"}
    assert _rule_matches("text", "logout, login", crate, None)
    assert not _rule_matches("text", "billing", crate, None)
    assert _rule_matches("source", "github, jira", crate, None)
    assert _rule_matches("risk", "medium+", crate, None) and not _rule_matches("risk", "high", crate, None)
    assert _rule_matches("repo", "app", crate, None)


def test_filtered_tester_runs_on_matching_crates_and_passes_the_rest(make_factory):
    layout = simple_layout(["coder", "tester"])
    machine(layout, "tester")["filter"] = {"match": "any", "rules": [{"when": "text", "value": "needs tests"}]}
    f = make_factory(layout=[layout])
    f.start()

    async def go():
        async with f.client() as c:
            skip = await c.submit("Tidy the readme")
            await c.until_state(skip, "shipped", "held", "scrapped")
            run = await c.submit("Add a parser, needs tests")
            await c.until_state(run, "shipped", "held", "scrapped")
            return skip, run, list(c.events)
    skip, run, events = asyncio.run(go())
    skipped, ran = item(f, skip), item(f, run)
    assert skipped["state"] == "shipped", "\n".join(map(str, skipped["log"][-14:]))
    assert any("Tester: doesn't match its filter (the spec mentions needs tests); passing straight through"
               in str(e) for e in skipped["log"])
    tester_jobs = {e.get("item_id") for e in events if e.get("type") == "job_started" and e.get("machine") == "tester"}
    assert skip not in tester_jobs and run in tester_jobs
    assert any("Tester: matches its filter" in str(e) for e in ran["log"])
