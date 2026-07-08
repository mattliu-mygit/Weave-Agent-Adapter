"""Second harness (Codex), proves the adapter is profile-only: no code path is
Claude-specific. Codex adds SubagentStart (real subagent span) and has no
SessionEnd (sessions close via the sweep)."""
from __future__ import annotations

import json

from conftest import NS, run, starts, one, end_of
from weave_agent_adapter.install import install


CX = "codex"
SID = "cx1"


def test_codex_traces_with_only_a_profile():
    tr, sink = run([
        ("SessionStart", {"session_id": SID, "source": "startup", "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "refactor it"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1",
                        "tool_input": {"command": "pytest"}}),
        ("PermissionRequest", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "u1",
                         "tool_response": {"code": 0}}),
        ("Stop", {"session_id": SID}),
    ], harness=CX)

    session = one(sink, f"{NS}.session")
    turn = one(sink, f"{NS}.turn")
    tool = one(sink, f"{NS}.tool.Bash")
    assert turn.parent_id == session.id
    assert tool.parent_id == turn.id
    # PermissionRequest recorded, then PostToolUse -> allow inferred
    assert end_of(sink, tool.id).attributes[NS]["permission_decision"] == "allow"


def test_codex_subagent_start_stop_is_a_real_span():
    # Codex HAS SubagentStart, so the subagent gets an open/close span (not a
    # stop-only annotation as on Claude Code).
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStart", {"session_id": SID, "agent_id": "ag1", "agent_type": "reviewer"}),
        ("SubagentStop", {"session_id": SID, "agent_id": "ag1", "agent_type": "reviewer",
                          "last_assistant_message": "looks good"}),
    ], harness=CX)
    agent = one(sink, f"{NS}.agent.reviewer")
    e = end_of(sink, agent.id)
    assert e is not None                          # opened AND closed -> a real span
    assert e.output == "looks good"


def test_codex_session_closes_via_sweep_without_session_end():
    # Codex emits no SessionEnd; the sweep must finalize the session.
    tr, sink = run([("SessionStart", {"session_id": SID})], t0=1000.0, harness=CX)
    assert tr.sessions                            # still open (no SessionEnd)
    tr.sweep(now=1000.0 + 10_000, ttl=60.0)
    assert not tr.sessions
    assert end_of(sink, one(sink, f"{NS}.session").id).output["incomplete"] is True


def test_codex_install_targets_codex_hooks_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    with open(path) as f:
        hooks = json.load(f)["hooks"]
    assert "SubagentStart" in hooks and "PermissionRequest" in hooks
    assert "SessionEnd" not in hooks               # Codex has none
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "--harness codex" in cmd and cmd.endswith("--event PreToolUse")
