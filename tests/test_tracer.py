"""Reducer behavior: event stream → finalized turns, sessions, sweep, threads."""
from __future__ import annotations

import json

from conftest import NS, run, subagents_of, tools_of
from weave_agent_adapter.core.model import ToolStatus

SID = "s1"


def test_turn_finalized_on_session_end():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "do it"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "ls"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"stdout": "ok"}}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, project_id), = turns
    assert project_id == "ent/proj"
    assert node["attributes"]["gen_ai.prompt.0.content"] == "do it"
    assert node["attributes"]["gen_ai.completion.0.content"] == "done"
    (tool,) = tools_of(node, "Bash")
    assert tool["attributes"][f"{NS}.tool.status"] == "ok"
    assert not tr.sessions                        # popped on session_end


def test_tool_correlation_by_use_id():
    # two tools open concurrently -> the close must match by tool_use_id
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a",
                         "tool_response": {"x": 1}}),
    ])
    t = tr.sessions[SID].current_turn
    assert t.tool_calls["a"].status == ToolStatus.OK
    assert t.tool_calls["b"].status == ToolStatus.RUNNING


def test_midturn_prompt_is_steering_not_new_turn():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "first"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "actually, wait"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns                            # one turn, not two
    (ev,) = node["events"]
    assert ev["name"] == "steering"
    assert ev["attributes"][f"{NS}.steering.text"] == "actually, wait"


def test_stop_only_subagent_gets_identity_no_output():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "agent_type": "Explore", "agent_id": "a9",
                          "last_assistant_message": "not reliably the subagent's"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    (sub,) = subagents_of(node, "Explore")
    assert sub["attributes"][f"{NS}.agent_id"] == "a9"
    assert "gen_ai.completion.0.content" not in sub["attributes"]


def test_background_subagent_stop_is_ignored():
    # Claude Code fires SubagentStop for its own background agents (prompt
    # suggestions, title gen) with no agent_type; those must not create a node.
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "last_assistant_message": "Yes, do it"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    assert subagents_of(node) == []


def test_background_stop_does_not_steal_real_subagent():
    # a background SubagentStop (different agent_id, no agent_type) must not close
    # the real tracked subagent; all its tools stay under one node.
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStart", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "a",
                        "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "a",
                         "agent_id": "g1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "bg99"}),   # background, no type
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b",
                        "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b",
                         "agent_id": "g1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose",
                          "last_assistant_message": "done"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    (sub,) = subagents_of(node)                   # exactly one subagent node
    assert len(tools_of(sub, "Bash")) == 2        # both tools inside it
    assert sub["attributes"]["gen_ai.completion.0.content"] == "done"


def test_compaction_recorded_as_turn_event():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreCompact", {"session_id": SID, "trigger": "auto"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    (ev,) = [e for e in node["events"] if e["name"] == "compaction"]
    assert ev["attributes"][f"{NS}.compaction.trigger"] == "auto"


def test_sampling_excludes_session():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SessionEnd", {"session_id": SID}),
    ], session_rate=0.0)
    assert turns == []
    assert not tr.sessions


def test_sweep_finalizes_stale_session_and_emits_pending_turn():
    tr, turns = run([("SessionStart", {"session_id": SID}),
                     ("UserPromptSubmit", {"session_id": SID, "prompt": "p"})], t0=1000.0)
    assert turns == []                            # turn still open, nothing emitted
    swept = tr.sweep(now=1000.0 + 10_000, ttl=60.0)
    assert swept == 1
    assert not tr.sessions
    (node, _), = turns
    assert node["attributes"][f"{NS}.incomplete"] == "true"


def test_turnless_session_emits_nothing():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    assert turns == []
    assert not tr.sessions


def test_session_autocreated_without_session_start():
    # resume/edit continue under a new session_id with no SessionStart; the turn
    # and its tools must still be traced (auto-created session).
    tr, turns = run([
        ("UserPromptSubmit", {"session_id": "resumed", "prompt": "keep going", "cwd": "/repo"}),
        ("PreToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"ok": 1}}),
        ("Stop", {"session_id": "resumed"}),
        ("SessionEnd", {"session_id": "resumed"}),
    ])
    (node, _), = turns
    assert node["attributes"]["gen_ai.prompt.0.content"] == "keep going"
    assert tools_of(node, "Bash")


def test_project_per_repo_routes_by_cwd_leaf():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/Users/me/my-repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "hi"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], project="default-proj", project_per_repo=True)
    (_, project_id), = turns
    assert project_id == "ent/my-repo"            # cwd leaf + default entity


def test_thread_id_from_transcript_root(tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join([
        json.dumps({"type": "queue-operation"}),                       # no uuid: skipped
        json.dumps({"type": "user", "isSidechain": False, "parentUuid": None,
                    "uuid": "ROOT-UUID", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "parentUuid": "ROOT-UUID"}),
    ]))
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": str(tp)}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "hi", "transcript_path": str(tp)}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    assert node["attributes"]["gen_ai.conversation.id"] == "ROOT-UUID"


def test_autocreated_session_creates_synthetic_turn_for_midturn_events():
    # sidecar restart: first event for this sid is a tool, not a turn_start;
    # a synthetic turn is created so the tool isn't dropped
    tr, turns = run([
        ("PreToolUse", {"session_id": "crash-resumed", "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": "crash-resumed", "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"ok": 1}}),
        ("Stop", {"session_id": "crash-resumed"}),
        ("SessionEnd", {"session_id": "crash-resumed"}),
    ])
    (node, _), = turns
    assert node["attributes"]["gen_ai.prompt.0.content"] == "(resumed)"
    assert tools_of(node, "Bash")


def test_subagent_stop_with_no_agent_id_ignored():
    # agent_type present but agent_id missing → must not key subagents[None]
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "agent_type": "Explore"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    assert subagents_of(node) == []


def test_redaction_applied_to_tool_input():
    from weave_agent_adapter.redact import Redactor
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"api_key": "supersecret", "command": "ls"}}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], redactor=Redactor())
    (node, _), = turns
    args = tools_of(node, "Bash")[0]["attributes"]["gen_ai.tool.call.arguments"]
    assert "supersecret" not in args
    assert "ls" in args
