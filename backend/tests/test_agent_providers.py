"""Other coding agents (Gemini CLI, Antigravity CLI, GitHub Copilot CLI) in place of Claude Code."""
import asyncio
import json
import os
import sys

from factory import agents, config
from factory.engine import _clean_pipeline

from .conftest import FAKES, IS_WINDOWS, item, simple_layout

CODER = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python -m pytest:*),Bash(git log:*)"]
READER = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--allowedTools", "Read,Glob,Grep"]
ALL = {"--model", "--effort", "--output-format", "--input-format", "--approval-mode", "--allowed-tools",
       "--allow-tool", "--no-ask-user", "--no-color", "stream-json"}


def test_a_machine_keeps_its_agent_and_drops_unknown_ones():
    catalog, _, _ = config.load_catalog()
    line = config.build_line(catalog, [
        {"type": "coder", "id": "coder", "agent": "gemini", "model": "flash"},
        {"type": "tester", "id": "tester", "agent": "gemini"},  # runs no agent
        {"type": "reviewer", "id": "reviewer", "agent": "skynet"},
    ])
    assert (line[0].agent, line[0].model) == ("gemini", "flash")
    assert line[1].agent == "" and line[2].agent == ""
    assert line[0].summary()["agent"] == "gemini"
    p = simple_layout()
    p["machines"][0].update(agent="copilot")
    assert _clean_pipeline(p)["machines"][0]["agent"] == "copilot"


def test_the_default_model_applies_only_to_the_default_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_FILE", tmp_path / "agents.json")
    agents.set_defaults("claude-opus-5-5", "high")
    assert agents.resolve_choice("", "", "") == ("claude", "claude-opus-5-5", "high")
    assert agents.resolve_choice("gemini", "", "") == ("gemini", "", "")
    assert agents.resolve_choice("gemini", "pro", "") == ("gemini", "pro", "")
    assert agents.resolve_choice("claude", "", "low") == ("claude", "claude-opus-5-5", "low")


def test_choices_list_every_agent():
    ids = [a["id"] for a in agents.choices()["agents"]]
    assert ids == ["claude", "gemini", "antigravity", "copilot"]
    assert agents.choices()["models"] == list(agents.MODELS)  # older games read these


def test_gemini_command():
    argv, stdin, notes = agents.translate("gemini", CODER, "do it", "flash", "high", ALL)
    assert argv == ["gemini", "--output-format", "stream-json", "--approval-mode", "auto_edit",
                    "--allowed-tools", "run_shell_command(python -m pytest),run_shell_command(git log)",
                    "-m", "flash"]
    assert stdin == "do it"
    assert len(notes) == 1 and "effort" in notes[0]
    argv, _, notes = agents.translate("gemini", READER, "look", "", "", ALL)
    assert argv == ["gemini", "--output-format", "stream-json"] and notes == []


def test_antigravity_command(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_FILE", tmp_path / "agents.json")
    argv, stdin, notes = agents.translate("antigravity", CODER, "do it", "m-1", "high", ALL)
    assert argv == ["agy", "--output-format", "stream-json", "--input-format", "stream-json",
                    "--model", "m-1", "--effort", "high"]
    assert json.loads(stdin) == {"event": "user", "message": {"content": "do it"}}
    assert len(notes) == 1 and "antigravity_skip_permissions" in notes[0]
    (tmp_path / "agents.json").write_text(json.dumps({"antigravity_skip_permissions": True}))
    argv, _, notes = agents.translate("antigravity", CODER, "do it", "", "", ALL)
    assert "--dangerously-skip-permissions" in argv and notes == []
    argv, _, notes = agents.translate("antigravity", READER, "look", "", "", ALL)
    assert "--dangerously-skip-permissions" not in argv and notes == []


def test_copilot_command():
    argv, stdin, notes = agents.translate("copilot", CODER, "do it", "gpt-5", "low", ALL)
    assert argv == ["copilot", "--output-format", "json", "--no-ask-user", "--no-color", "--allow-tool",
                    "read,write,shell(python -m pytest:*),shell(git log:*)", "--model", "gpt-5"]
    assert stdin == "do it" and len(notes) == 1
    argv, _, _ = agents.translate("copilot", READER, "look", "", "", ALL)
    assert argv[argv.index("--allow-tool") + 1] == "read"


def test_an_old_cli_gets_only_the_flags_it_has():
    argv, _, notes = agents.translate("gemini", CODER, "x", "", "", {"--output-format"})
    assert argv == ["gemini", "--output-format", "json"]
    assert len(notes) == 2  # no --approval-mode, no --allowed-tools


def _feed(parser, events):
    shown = []
    for e in events:
        shown += parser.feed(e if isinstance(e, str) else json.dumps(e))
    return shown, parser.finish()


def test_gemini_output():
    shown, result = _feed(agents.parser_for("gemini"), [
        {"type": "init", "model": "gemini-x"},
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "message", "role": "assistant", "content": "Let me ", "delta": True},
        {"type": "message", "role": "assistant", "content": "look.", "delta": True},
        {"type": "tool_use", "tool_name": "read_file", "parameters": {"absolute_path": "a.py"}},
        {"type": "tool_result", "status": "success"},
        {"type": "message", "role": "assistant", "content": "Verdict: Looks good\n- fine", "delta": True},
        {"type": "result", "status": "success", "stats": {"total_tokens": 99, "duration_ms": 2000}},
    ])
    assert result.text == "Verdict: Looks good\n- fine" and result.tokens == 99 and result.turns == 2
    assert result.cost_usd == 0
    assert "Let me look." in shown and "  > read_file a.py" in shown and "Verdict: Looks good" in shown


def test_gemini_json_output():
    _, result = _feed(agents.parser_for("gemini"), [
        {"response": "Ready.", "stats": {"models": {"g": {"tokens": {"total": 7}}}}}])
    assert (result.text, result.tokens) == ("Ready.", 7)


def test_antigravity_output():
    shown, result = _feed(agents.parser_for("antigravity"), [
        {"type": "init", "model": "m"},
        {"type": "step_update", "step_index": 2, "step_type": "tool", "state": "DONE", "tool_name": "view_file",
         "tool_info": {"path": "x.py"}},
        {"type": "step_update", "step_index": 3, "step_type": "agent_response", "state": "ACTIVE", "text_delta": "Ri"},
        {"type": "step_update", "step_index": 3, "step_type": "agent_response", "state": "DONE", "text_delta": "sk: Low"},
        {"type": "result", "status": "SUCCESS", "response": "Risk: Low", "num_turns": 3,
         "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 1, "total_tokens": 16}},
    ])
    assert (result.text, result.turns, result.tokens, result.error) == ("Risk: Low", 3, 16, "")
    assert "Risk: Low" in shown and "  > view_file x.py" in shown


def test_copilot_output_and_plain_text():
    shown, result = _feed(agents.parser_for("copilot"), [
        {"type": "assistant.message", "data": {"content": "Checking."}},
        {"type": "tool.execution_start", "data": {"toolName": "bash", "arguments": {"command": "git log"}}},
        {"type": "assistant.usage", "data": {"inputTokens": 40, "outputTokens": 2}},
        {"type": "assistant.message", "data": {"content": "Ready: yes"}},
        {"type": "result", "exitCode": 0, "usage": {"sessionDurationMs": 3000}},
    ])
    assert (result.text, result.turns, result.tokens) == ("Ready: yes", 2, 42)
    assert "  > bash git log" in shown
    # A Copilot CLI too old for --output-format: its answer is what it printed.
    _, result = _feed(agents.parser_for("copilot"), ["Ready: yes", "", "all good"])
    assert result.text == "Ready: yes\nall good"


def _agent_shims(factory) -> None:
    bin_dir = factory.root / "bin"
    for exe in ("gemini", "agy", "copilot"):
        script = FAKES / "fake_agent.py"
        if IS_WINDOWS:
            (bin_dir / f"{exe}.cmd").write_text(f'@"{sys.executable}" "{script}" as={exe} %*\r\n', encoding="utf-8")
        else:
            path = bin_dir / exe
            path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" as={exe} "$@"\n', encoding="utf-8")
            path.chmod(0o755)


def test_a_line_runs_on_other_agents_and_records_them(make_factory):
    layout = simple_layout(["refiner", "coder", "tester", "reviewer", "packager"])
    layout["machines"][1].update(agent="gemini", model="flash")
    layout["machines"][3].update(agent="copilot")
    factory = make_factory(layout=[layout])
    (factory.backend / "agents.json").write_text(json.dumps({"agent": "antigravity", "effort": "low"}))
    _agent_shims(factory)
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Add a thing")
            await c.until_state(n, "shipped", "held", "scrapped", timeout=120)
            await c.send(type="stats", days=7)
            return n, await c.wait_for(lambda e: e.get("type") == "stats")
    n, stats = asyncio.run(go())
    crate = item(factory, n)
    assert crate["state"] == "shipped", crate["log"][-15:]
    gemini = [json.loads(x) for x in (factory.state / "gemini_args.jsonl").read_text().splitlines()]
    assert gemini[0][-2:] == ["-m", "flash"] and "auto_edit" in gemini[0]
    agy = [json.loads(x) for x in (factory.state / "agy_args.jsonl").read_text().splitlines()]
    agy = [a for a in agy if a != ["models"]]  # the game's model list asks `agy models`
    assert agy[0][-2:] == ["--effort", "low"]  # the Refiner, on the factory default
    assert (factory.state / "copilot_args.jsonl").exists()
    # fake_agent.py answers with fake_claude.py, run with just -p: no machine ran claude itself.
    claude = [json.loads(x) for x in (factory.state / "claude_args.jsonl").read_text().splitlines()]
    assert all(a == ["-p"] for a in claude)
    picks = {(a["agent"], a["model"], a["effort"]): a for a in stats["agents"]}
    assert ("gemini", "flash", "default") in picks
    assert ("antigravity", "default", "low") in picks
    assert ("copilot", "default", "default") in picks
    assert picks[("gemini", "flash", "default")]["tokens"] == 1200
    assert all(a["cost_usd"] == 0 for a in stats["agents"])
