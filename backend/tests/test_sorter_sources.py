"""Validation follows a Sorter's "Came from" / "Arrived as" rules: each output is checked only
with the intake's sources whose crates can go out of it."""
from factory import graph
from factory.config import load_catalog


def sorted_line(rules):
    """Issues and open PRs into a Sorter: out_1 to a Coder line, out_3 to CI."""
    return {
        "id": "dist", "name": "Distribution", "sources": [],
        "storages": [{"id": "store_1", "source": "github", "repo": "me/app", "pos": [0, 3]},
                     {"id": "store_2", "source": "github_pr", "repo": "me/app", "pos": [0, 5]}],
        "intake": {"pos": [0, 0]}, "dock": {"pos": [20, 0]}, "scrap": {"pos": [20, 3]},
        "machines": [
            {"id": "sorter", "type": "sorter", "pos": [3, 0], "rules": rules},
            {"id": "coder", "type": "coder", "pos": [6, 0]},
            {"id": "packager", "type": "packager", "pos": [9, 0]},
            {"id": "ci", "type": "ci", "pos": [12, 3]},
        ],
        "belts": [
            {"from": "store_1", "port": "out", "to": "intake"},
            {"from": "store_2", "port": "out", "to": "intake"},
            {"from": "intake", "port": "out", "to": "sorter"},
            {"from": "sorter", "port": "out_1", "to": "coder"},
            {"from": "coder", "port": "pass", "to": "packager"},
            {"from": "packager", "port": "pass", "to": "dock"},
            {"from": "sorter", "port": "out_3", "to": "ci"},
            {"from": "ci", "port": "pass", "to": "dock"},
        ],
    }


def problems(rules):
    catalog, _, _ = load_catalog()
    fatal, found = graph.check(catalog, [sorted_line(rules)])
    assert fatal is None
    return found


def test_came_from_keeps_issues_off_the_ci_output():
    rules = [{"port": "out_1", "when": "source", "value": "github"},
             {"port": "out_3", "when": "source", "value": "github_pr"}]
    assert problems(rules) == {}


def test_everything_else_gets_the_sources_earlier_outputs_left():
    rules = [{"port": "out_1", "when": "source", "value": "github"},
             {"port": "out_3", "when": "otherwise", "value": ""}]
    assert problems(rules) == {}


def test_an_output_issues_can_reach_still_needs_a_packager_before_ci():
    rules = [{"port": "out_1", "when": "source", "value": "github_pr"},
             {"port": "out_3", "when": "otherwise", "value": ""}]
    assert "CI needs" in problems(rules)["dist"]


def test_a_rule_that_depends_on_the_crate_keeps_every_source():
    rules = [{"port": "out_1", "when": "text", "value": "docs"},
             {"port": "out_3", "when": "otherwise", "value": ""}]
    assert "CI needs" in problems(rules)["dist"]


def test_arrived_as_failed_is_never_true_straight_from_the_intake():
    rules = [{"port": "out_1", "when": "result", "value": "fail"},
             {"port": "out_3", "when": "source", "value": "github_pr"}]
    # Nothing failed before the sorter, so issues never reach out_1 (fine) and can't reach out_3.
    assert problems(rules) == {}
