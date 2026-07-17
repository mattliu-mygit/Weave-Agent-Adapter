"""Gemini CLI uses the same reducer through a declarative profile."""
from __future__ import annotations

import json

from conftest import run, subagents_of, tools_of
from weave_agent_adapter.install import install
from weave_agent_adapter.model import ToolStatus
from weave_agent_adapter.profile import load_profile


def test_gemini_traces_stable_non_streaming_hooks():
    tr, turns = run([
        ("SessionStart", {"session_id": "gm1", "cwd": "/repo"}),
        ("BeforeAgent", {"session_id": "gm1", "prompt": "fix it"}),
        ("BeforeModel", {"session_id": "gm1", "llm_request": {"model": "gemini-2.5-pro"}}),
        ("BeforeTool", {"session_id": "gm1", "tool_name": "run_shell_command",
                        "tool_input": {"command": "printf ok"}}),
        ("AfterTool", {"session_id": "gm1", "tool_name": "run_shell_command",
                       "tool_input": {"command": "printf ok"},
                       "tool_response": {"output": "ok"}}),
        ("PreCompress", {"session_id": "gm1", "trigger": "auto"}),
        ("AfterAgent", {"session_id": "gm1", "prompt_response": "done"}),
        ("SessionEnd", {"session_id": "gm1", "reason": "exit"}),
    ], harness="gemini-cli")

    (turn, session), = turns
    assert session.harness == "gemini-cli"
    assert turn.input_text == "fix it"
    assert turn.output_text == "done"
    assert turn.model == "gemini-2.5-pro"
    assert turn.compactions == [(1005.0, "auto")]
    (tool,) = tools_of(turn, "run_shell_command")
    assert tool.status == ToolStatus.OK
    assert tool.output == {"output": "ok"}
    assert subagents_of(turn) == []
    assert not tr.sessions


def test_gemini_embedded_tool_error_is_canonical_error():
    _, turns = run([
        ("BeforeAgent", {"session_id": "gm2", "prompt": "run it"}),
        ("BeforeTool", {"session_id": "gm2", "tool_name": "run_shell_command",
                        "tool_input": {"command": "false"}}),
        ("AfterTool", {"session_id": "gm2", "tool_name": "run_shell_command",
                       "tool_input": {"command": "false"},
                       "tool_response": {"error": "exit status 1"}}),
        ("AfterAgent", {"session_id": "gm2", "prompt_response": "failed"}),
    ], harness="gemini-cli")

    (turn, _), = turns
    (tool,) = tools_of(turn)
    assert tool.status == ToolStatus.ERROR
    assert tool.error == "exit status 1"
    assert tool.output is None


def test_gemini_profile_omits_unsupported_optional_capabilities():
    profile = load_profile("gemini-cli")

    assert "PermissionRequest" not in profile.events
    assert "SubagentStart" not in profile.events
    assert profile.enrich == {}
    assert profile.config_surface == {}
    assert profile.registration["command"].endswith("--success-json")


def test_gemini_install_targets_settings_json_and_requests_json_success(tmp_path):
    path = str(tmp_path / "settings.json")
    install("gemini-cli", path=path)

    hooks = json.loads((tmp_path / "settings.json").read_text())["hooks"]
    command = hooks["BeforeAgent"][0]["hooks"][0]["command"]
    assert "--harness gemini-cli --success-json" in command
    assert command.endswith("--event BeforeAgent")
