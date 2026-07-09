"""Second harness (Codex), proves the adapter is profile-only: no code path is
Claude-specific. Codex has SubagentStart (real open/close subagent) and no
SessionEnd (sessions finalize via the sweep)."""
from __future__ import annotations

import json

from conftest import run, subagents_of, tools_of
from weave_agent_adapter.install import install

CX = "codex"
SID = "cx1"


def test_codex_traces_with_only_a_profile():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "source": "startup", "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "refactor it"}),
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
    assert turns == []                            # no SessionEnd: pending until sweep
    tr.sweep(now=10_000.0, ttl=1.0)
    (node, _), = turns
    assert node["name"] == "invoke_agent codex"
    assert tools_of(node, "Bash")
    (sub,) = subagents_of(node, "reviewer")
    assert sub["attributes"]["gen_ai.completion.0.content"] == "looks good"


def test_codex_install_targets_codex_hooks_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    with open(path) as f:
        hooks = json.load(f)["hooks"]
    assert "SubagentStart" in hooks and "PermissionRequest" in hooks
    assert "SessionEnd" not in hooks               # Codex has none
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "--harness codex" in cmd and cmd.endswith("--event PreToolUse")
