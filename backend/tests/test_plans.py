"""Plan records: tasks released as the ones they wait for ship, scraps recorded, and the plan
reported finished once every task is shipped or scrapped."""
import json

from factory import plans


def _plan(tmp_path, tracking=35):
    folder = tmp_path / "plans"
    folder.mkdir()
    record = {"repo": "me/repo", "tracking": tracking, "tasks": {
        "T1": {"number": 36, "depends_on": [], "labelled": True, "shipped": False},
        "T2": {"number": 37, "depends_on": ["T1"], "labelled": False, "shipped": False},
        "T3": {"number": 38, "depends_on": ["T2"], "labelled": False, "shipped": False},
        "T4": {"number": 39, "depends_on": [], "labelled": True, "shipped": False},
    }}
    (folder / "crate-1.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def test_plan_finishes_once_every_task_ships(tmp_path):
    folder = _plan(tmp_path)
    assert plans.shipped(folder, "me/repo", 36) == [37]
    plans.labelled(folder, "me/repo", 37)
    assert plans.finished(folder, "me/repo", 36) == []
    for n in (37, 38, 39):
        plans.shipped(folder, "me/repo", n)
    assert plans.finished(folder, "me/repo", 39) == [{"tracking": 35, "shipped": [36, 37, 38, 39], "scrapped": []}]
    plans.closed(folder, "me/repo", 35)
    assert plans.finished(folder, "me/repo", 39) == []  # closed once, not again
    assert plans.finished(folder, "other/repo", 39) == []


def test_a_scrapped_task_names_what_waits_for_it(tmp_path):
    folder = _plan(tmp_path)
    plans.shipped(folder, "me/repo", 36)
    # T3 waits for T2 too, through nothing else: both are reported.
    assert plans.scrapped(folder, "me/repo", 37) == [{"tracking": 35, "blocked": [38]}]
    plans.shipped(folder, "me/repo", 39)
    assert plans.finished(folder, "me/repo", 39) == []  # #38 is still open, waiting
    plans.scrapped(folder, "me/repo", 38)
    assert plans.finished(folder, "me/repo", 38) == [{"tracking": 35, "shipped": [36, 39], "scrapped": [37, 38]}]


def test_a_scrapped_task_sent_in_again_and_shipped_releases_the_rest(tmp_path):
    folder = _plan(tmp_path)
    plans.shipped(folder, "me/repo", 36)
    plans.scrapped(folder, "me/repo", 37)
    assert plans.shipped(folder, "me/repo", 37) == [38]


def test_a_plan_without_a_tracking_issue_has_nothing_to_close(tmp_path):
    folder = _plan(tmp_path, tracking=None)
    assert plans.scrapped(folder, "me/repo", 36) == []
    for n in (37, 38, 39):
        plans.shipped(folder, "me/repo", n)
    assert plans.finished(folder, "me/repo", 39) == []
