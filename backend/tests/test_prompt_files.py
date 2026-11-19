"""Machine prompts live in backend/prompts/<machine>/*.md and are read again on every run."""
from factory.config import tomllib

from factory import config, prompts
from factory.config import BACKEND_DIR
from factory.engine import drop_default_prompts


def _catalog():
    return config.load_catalog()[0]


def _machine(kind, prompt=None):
    entry = {"type": kind, "id": kind, **({"prompt": prompt} if prompt is not None else {})}
    return config.build_line(_catalog(), [entry])[0]


def test_machines_toml_holds_no_prompt_text():
    raw = tomllib.loads((BACKEND_DIR / "machines.toml").read_text(encoding="utf-8"))
    for t in raw["types"]:
        assert "prompt" not in t, f"{t['id']}'s prompt belongs in backend/prompts/"
        for st in t.get("steps", []):
            if st["command"][:1] == ["claude"]:
                assert st.get("stdin_file") or st.get("stdin") == "{prompt}", (t["id"], st["name"])
    files = {t.get("prompt_file") for t in raw["types"]} | {
        st.get("stdin_file") for t in raw["types"] for st in t.get("steps", [])}
    for rel in files - {None}:
        assert (BACKEND_DIR / "prompts" / rel).read_text(encoding="utf-8").strip(), rel


def test_every_machine_with_a_prompt_loads_it_from_its_file():
    for kind, m in _catalog().items():
        if m.has_prompt():
            assert m.prompt_file == f"{kind}/prompt.md"
            assert m.current_prompt() == (prompts.PROMPTS_DIR / m.prompt_file).read_text(encoding="utf-8")


def test_editing_a_prompt_file_reaches_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    (tmp_path / "coder").mkdir()
    (tmp_path / "coder" / "prompt.md").write_text("First version {spec}\n", encoding="utf-8")
    (tmp_path / "coder" / "write_code.md").write_text("{prompt}\nNo tests.\n", encoding="utf-8")
    m = config.MachineConfig(id="coder", name="Coder", prompt="First version {spec}\n",
                             prompt_file="coder/prompt.md", default_prompt="First version {spec}\n",
                             steps=[config.StepConfig(name="Write code", command=["claude"], stdin="{prompt}\nNo tests.\n",
                                                      stdin_file="coder/write_code.md")])
    (tmp_path / "coder" / "prompt.md").write_text("Second version {spec}\n", encoding="utf-8")
    (tmp_path / "coder" / "write_code.md").write_text("{prompt}\nStill no tests.\n", encoding="utf-8")
    assert m.current_prompt() == "Second version {spec}\n"
    assert m.steps[0].stdin_text() == "{prompt}\nStill no tests.\n"
    assert m.is_default_prompt("Second   version\n{spec}")
    (tmp_path / "coder" / "prompt.md").unlink()  # gone: the text it had at start
    assert m.current_prompt() == "First version {spec}\n"


def test_a_prompt_edited_in_the_game_wins_over_the_file():
    m = _machine("refiner", "My own refiner prompt.\n{text}")
    assert m.current_prompt() == "My own refiner prompt.\n{text}"
    default = _machine("refiner", _catalog()["refiner"].default_prompt + "\n")  # a saved copy of the default
    assert default.current_prompt() == _catalog()["refiner"].current_prompt()


def test_saved_copies_of_the_default_prompt_are_dropped():
    catalog = _catalog()
    pipelines = [{"machines": [
        {"id": "refiner", "type": "refiner", "prompt": catalog["refiner"].default_prompt},
        {"id": "coder", "type": "coder", "prompt": "Mine.\n{spec}"},
        {"id": "tester", "type": "tester", "prompt": "unused"}]}]
    assert drop_default_prompts(catalog, pipelines)
    assert [m.get("prompt") for m in pipelines[0]["machines"]] == [None, "Mine.\n{spec}", None]
    assert not drop_default_prompts(catalog, pipelines)
