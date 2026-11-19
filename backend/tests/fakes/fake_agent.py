"""Stands in for the Gemini, Antigravity and Copilot CLIs in the backend's tests: records its
options, runs fake_claude.py with the same prompt, and prints fake_claude's answer the way that
CLI would. Its first argument says which CLI it is."""
import json
import os
import subprocess
import sys
from pathlib import Path

name = sys.argv.pop(1).removeprefix("as=")  # the test's shim passes as=gemini, as=agy or as=copilot
state = Path(os.environ.get("FAKE_STATE", "."))
HELP = {"gemini": "--model --output-format stream-json --approval-mode --allowed-tools",
        "agy": "--model --effort --output-format --input-format stream-json",
        "copilot": "--model --output-format --allow-tool --no-ask-user --no-color"}
if "--help" in sys.argv:
    print(f"Usage: {name} [options]\n  {HELP[name]}")
    sys.exit(0)
with open(state / f"{name}_args.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
prompt = sys.stdin.read()
if name == "agy":
    prompt = json.loads(prompt.splitlines()[0])["message"]["content"]
claude = Path(__file__).with_name("fake_claude.py")
out = subprocess.run([sys.executable, str(claude), "-p"], input=prompt, capture_output=True, text=True)
result = next((json.loads(l) for l in out.stdout.splitlines() if l.startswith("{") and '"result"' in l), {})
text, ok = str(result.get("result", "")), not result.get("is_error")


def emit(obj):
    print(json.dumps(obj), flush=True)


if name == "gemini":
    emit({"type": "init", "session_id": "s1", "model": "gemini-x"})
    emit({"type": "tool_use", "tool_name": "read_file", "parameters": {"absolute_path": "README.md"}})
    half = len(text) // 2
    emit({"type": "message", "role": "assistant", "content": text[:half], "delta": True})
    emit({"type": "message", "role": "assistant", "content": text[half:], "delta": True})
    emit({"type": "result", "status": "success" if ok else "error",
          "stats": {"total_tokens": 1200, "input_tokens": 1000, "output_tokens": 200, "duration_ms": 50}})
elif name == "agy":
    emit({"type": "init", "cwd": os.getcwd(), "model": "agy-model"})
    emit({"type": "step_update", "step_index": 1, "step_type": "agent_response", "state": "ACTIVE", "text_delta": text})
    emit({"type": "step_update", "step_index": 1, "step_type": "agent_response", "state": "DONE", "text_delta": ""})
    emit({"type": "result", "status": "SUCCESS" if ok else "ERROR", "response": text, "num_turns": 2,
          "usage": {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330}})
else:
    emit({"type": "assistant.message", "data": {"messageId": "m1", "content": "Looking.",
                                                 "toolRequests": [{"name": "view"}]}})
    emit({"type": "tool.execution_start", "data": {"toolCallId": "t1", "toolName": "view", "arguments": {"path": "."}}})
    emit({"type": "assistant.message", "data": {"messageId": "m2", "content": text}})
    emit({"type": "result", "exitCode": 0 if ok else 1, "usage": {"sessionDurationMs": 40}})
sys.exit(out.returncode)
