"""The Coder's modes: Implement (the default, as before), TDD and Test writer only."""
from factory import config
from factory.engine import _skip_reason


def coder(options=None):
    catalog, _, _ = config.load_catalog()
    entry = {"type": "coder", "id": "coder"}
    if options is not None:
        entry["options"] = options
    return config.build_line(catalog, [entry])[0]


def steps_run(machine):
    return [s.name for s in machine.steps if not _skip_reason(s.when, machine.options, {})]


def test_a_coder_saved_before_modes_stays_on_implement():
    m = coder()
    assert m.options["coder_mode"] == "implement"
    assert steps_run(m) == ["Sync", "Make branch", "Bring in the base branch", "Write code",
                            "Stage changes", "Check for changes", "Commit"]
    assert coder({"coder_mode": "nonsense"}).options["coder_mode"] == "implement"


def test_implement_writes_no_tests_even_with_an_old_saved_prompt():
    m = coder({"coder_mode": "implement"})
    write = next(s for s in m.steps if s.name == "Write code")
    assert "{prompt}" in write.stdin and "implementation code only" in write.stdin
    assert "Add or update tests" not in m.prompt
    label = next(c["label"] for o in m.option_specs if o["id"] == "coder_mode"
                 for c in o["choices"] if c["value"] == "implement")
    assert "no tests" in label


def test_tdd_writes_tests_checks_they_fail_then_codes():
    m = coder({"coder_mode": "tdd"})
    assert steps_run(m) == ["Sync", "Make branch", "Bring in the base branch", "Write failing tests",
                            "Stage tests", "Check only tests changed", "Commit tests", "Check tests fail",
                            "Make the tests pass", "Stage changes", "Check for changes", "Commit"]
    first = {s.name for s in m.steps if s.first_pass_only}
    assert {"Write failing tests", "Commit tests", "Check tests fail"} <= first  # not again on rework
    assert "Make the tests pass" not in first


def test_test_writer_only_writes_no_code():
    m = coder({"coder_mode": "test_only"})
    assert steps_run(m) == ["Sync", "Make branch", "Bring in the base branch", "Write tests",
                            "Stage changes", "Check for changes", "Commit"]
    assert m.prompt_when == "coder_mode=implement|tdd" and "test_only" in m.prompt_notes


def test_the_game_lists_only_the_steps_the_mode_runs():
    for mode in ["implement", "tdd", "test_only"]:
        m = coder({"coder_mode": mode})
        assert m.summary()["steps"] == steps_run(m), mode
    assert "Write tests" not in coder({"coder_mode": "tdd"}).summary()["steps"]
    assert "Write code" not in coder({"coder_mode": "tdd"}).summary()["steps"]
