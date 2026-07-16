"""Codex exercises the same reducer through its declarative profile.

Codex exposes explicit subagent lifecycle events and no SessionEnd; Stop emits
a normal turn immediately, while the TTL sweep remains crash safety for turns
that never receive Stop.
"""
from __future__ import annotations

import json

from conftest import run, subagents_of, tools_of
from weave_agent_adapter.install import install

CX = "codex"
SID = "cx1"


def test_codex_traces_with_only_a_profile():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "source": "startup", "cwd": "/repo",
                          "model": "gpt-5.4", "permission_mode": "default"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "refactor it",
                              "model": "gpt-5.4", "permission_mode": "plan",
                              "turn_id": "turn-1"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1",
                        "tool_input": {"command": "pytest"}}),
        ("PermissionRequest", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1",
                         "tool_response": {"code": 0}}),
        ("SubagentStart", {"session_id": SID, "agent_id": "ag1", "agent_type": "reviewer"}),
        ("SubagentStop", {"session_id": SID, "agent_id": "ag1", "agent_type": "reviewer",
                          "last_assistant_message": "looks good"}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done"}),
    ], harness=CX)
    (turn, session), = turns
    assert session.harness == "codex"
    assert turn.model == "gpt-5.4"
    assert turn.permission_mode == "plan"
    assert turn.turn_id == "turn-1"
    assert tools_of(turn, "Bash")
    (sub,) = subagents_of(turn, "reviewer")
    assert sub["agent_id"] == "ag1"
    assert sub["output"] == "looks good"


def test_codex_install_targets_codex_hooks_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    with open(path) as f:
        hooks = json.load(f)["hooks"]
    assert "SubagentStart" in hooks and "PermissionRequest" in hooks
    assert "SessionEnd" not in hooks               # Codex has none
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "--harness codex" in cmd and cmd.endswith("--event PreToolUse")
