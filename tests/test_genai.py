"""Emitted turn-node shape: GenAI conventions, async subagents, thread sources."""
from __future__ import annotations

from conftest import NS, run, subagents_of, tools_of
from weave_agent_adapter.core.model import WireEvent
from weave_agent_adapter.emit import GenAITurnEmitter
from weave_agent_adapter.profile import Profile
from weave_agent_adapter.tracer import Tracer

SID = "s1"


def test_turn_node_follows_genai_conventions():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "add tests"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "pytest"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"exit": 0}}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done, 3 tests added"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t, _), = turns
    assert t["name"] == "invoke_agent claude-code"          # harness from the session
    a = t["attributes"]
    assert a["gen_ai.operation.name"] == "invoke_agent"
    assert a["gen_ai.conversation.id"]                       # thread id or session id
    (tool,) = tools_of(t, "Bash")
    ta = tool["attributes"]
    assert ta["gen_ai.operation.name"] == "execute_tool"
    assert ta["gen_ai.tool.call.id"] == "t1"
    assert "pytest" in ta["gen_ai.tool.call.arguments"]
    assert "exit" in ta["gen_ai.tool.call.result"]
    assert t["end"] > t["start"]                             # hook-captured timing preserved


def test_turn_root_has_weave_conversation_contract():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "add tests"}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    attrs = turns[0][0]["attributes"]
    assert attrs["wandb.thread_id"] == attrs["gen_ai.conversation.id"]
    assert attrs["wandb.is_turn"] is True
    assert attrs["input.value"] == "add tests"
    assert attrs["output.value"] == "done"


def test_async_subagent_after_stop_included_in_turn():
    # SubagentStart + interior tools + SubagentStop can all land AFTER the turn's
    # Stop; emission waits for finalization, so they appear inside the turn node.
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "l1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "l1",
                         "tool_response": {}}),
        ("Stop", {"session_id": SID}),                       # turn ends, subagent still running
        ("SubagentStart", {"session_id": SID, "agent_id": "a1", "agent_type": "Explore"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                        "agent_id": "a1", "agent_type": "Explore"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                         "agent_id": "a1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "a1", "agent_type": "Explore",
                          "last_assistant_message": "found it"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t, _), = turns
    (sub,) = subagents_of(t, "Explore")
    assert tools_of(sub, "Read")                             # interior tool nested inside
    assert sub["attributes"]["gen_ai.completion.0.content"] == "found it"
    assert sub["end"] > sub["start"]
    assert t["end"] >= sub["end"]
    assert t["end"] >= max(child["end"] for child in t["children"])


def test_one_trace_per_turn_shared_conversation():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "one"}),
        ("Stop", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "two"}),   # finalizes turn 1
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),                          # finalizes turn 2
    ])
    assert len(turns) == 2                                   # one trace per turn (precedent)
    convs = {t["attributes"]["gen_ai.conversation.id"] for t, _ in turns}
    assert len(convs) == 1                                   # stitched by conversation id
    assert turns[0][0]["attributes"]["gen_ai.prompt.0.content"] == "one"


def test_denied_tool_marked_error_with_reason():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PermissionDenied", {"session_id": SID, "tool_use_id": "t1",
                              "denial_reason": "nope"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t, _), = turns
    (tool,) = tools_of(t, "Bash")
    assert tool["error"] is True
    assert tool["attributes"][f"{NS}.permission.decision"] == "deny"
    assert tool["attributes"][f"{NS}.permission.denial_reason"] == "nope"


def test_large_tool_output_is_truncated():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "t1",
                         "tool_response": {"content": "x" * 50_000}}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t, _), = turns
    result = tools_of(t, "Read")[0]["attributes"]["gen_ai.tool.call.result"]
    assert len(result) < 40_000
    assert result.endswith("…[truncated]")


def test_failed_tool_emits_redacted_error_result():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUseFailure", {"session_id": SID, "tool_use_id": "t1",
                                "tool_response": "failed with sk-ABCDEFGHIJKLMNOP1234"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    result = tools_of(turns[0][0], "Bash")[0]["attributes"]["gen_ai.tool.call.result"]
    assert "failed with" in result
    assert "sk-ABCDEFGHIJKLMNOP1234" not in result


def test_bare_project_without_entity_stays_bare():
    # if entity is empty, project_id should be bare (not "/project")
    emitter = GenAITurnEmitter(default_entity="", emit=lambda n, p: None)
    from weave_agent_adapter.core.model import Session
    s = Session(session_id="s", project="my-proj", started_at=0, last_activity=0)
    pid = emitter._project_id(s)
    assert not pid.startswith("/")
    assert pid == "my-proj"


def test_turn_counters_stamped_and_filterable():
    # friction counters live on the turn root as ints (0 included) so the
    # signals/spans query layer can filter on them — span events aren't filterable
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "do it"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PermissionDenied", {"session_id": SID, "tool_use_id": "t1",
                              "denial_reason": "no"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "try X instead"}),  # steering
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t2"}),
        ("PostToolUseFailure", {"session_id": SID, "tool_use_id": "t2",
                                "tool_response": "boom"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "t3"}),
        ("PostToolUse", {"session_id": SID, "tool_use_id": "t3", "tool_response": {}}),
        ("Stop", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "clean turn"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t1, _), (t2, _) = turns
    a1 = t1["attributes"]
    assert a1[f"{NS}.steering_count"] == 1
    assert a1[f"{NS}.denial_count"] == 1
    assert a1[f"{NS}.tool_error_count"] == 1
    a2 = t2["attributes"]
    assert a2[f"{NS}.steering_count"] == 0                   # present even when zero
    assert a2[f"{NS}.denial_count"] == 0
    assert a2[f"{NS}.tool_error_count"] == 0


def test_effort_level_stamped_from_payload():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "effort": {"level": "xhigh"}}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (t, _), = turns
    assert t["attributes"][f"{NS}.effort_level"] == "xhigh"


def test_config_version_stamped_on_every_turn(tmp_path):
    # the A/B key: profile declares which artifacts form the config surface;
    # the fingerprint is computed per session and stamped on each turn root
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("be concise")
    prof = Profile(
        name="thirdparty", adapter="command-hook",
        events={"SessionStart": "session_start", "UserPromptSubmit": "turn_start",
                "Stop": "turn_end", "SessionEnd": "session_end"},
        fields={"session_id": "session_id", "prompt": "prompt"},
        registration={}, config_surface={"paths": [str(claude_md)]},
    )
    nodes = []
    em = GenAITurnEmitter(default_entity="ent", emit=lambda n, p: nodes.append(n))
    tr = Tracer(prof, "p", turn_emitters=[em])
    for i, (name, p) in enumerate([
            ("SessionStart", {"session_id": "s"}),
            ("UserPromptSubmit", {"session_id": "s", "prompt": "one"}),
            ("Stop", {"session_id": "s"}),
            ("UserPromptSubmit", {"session_id": "s", "prompt": "two"}),
            ("Stop", {"session_id": "s"}),
            ("SessionEnd", {"session_id": "s"})]):
        tr.handle(WireEvent(1, "thirdparty", name, 1.0 + i, p, 1))
    assert len(nodes) == 2
    from weave_agent_adapter.config_surface import config_version
    expected = config_version([str(claude_md)])
    assert all(n["attributes"][f"{NS}.config_version"] == expected for n in nodes)


def test_no_config_surface_means_no_attr():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),
    ], harness="codex")
    tr.sweep(now=10_000.0, ttl=1.0)
    (t, _), = turns
    assert f"{NS}.config_version" not in t["attributes"]


def test_thread_id_from_profile_field_source():
    # a harness that exposes a conversation id directly in the payload: profile
    # declares thread.source="field"; no transcript read, no Claude assumptions.
    turns = []
    prof = Profile(
        name="thirdparty", adapter="command-hook",
        events={"SessionStart": "session_start", "UserPromptSubmit": "turn_start",
                "Stop": "turn_end", "SessionEnd": "session_end"},
        fields={"session_id": "session_id", "prompt": "prompt", "conv": "conversation_id"},
        registration={}, thread={"source": "field", "id_field": "conv"},
    )
    em = GenAITurnEmitter(default_entity="ent", emit=lambda n, p: turns.append(n))
    tr = Tracer(prof, "p", turn_emitters=[em])
    for i, (name, p) in enumerate([
            ("SessionStart", {"session_id": "s", "conversation_id": "CONV-9"}),
            ("UserPromptSubmit", {"session_id": "s", "prompt": "hi"}),
            ("Stop", {"session_id": "s"}),
            ("SessionEnd", {"session_id": "s"})]):
        tr.handle(WireEvent(1, "thirdparty", name, 1.0 + i, p, 1))
    (node,) = turns
    assert node["attributes"]["gen_ai.conversation.id"] == "CONV-9"
