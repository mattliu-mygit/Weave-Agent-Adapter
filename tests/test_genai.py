"""GenAI dual-emit: each closed turn becomes one turn-rooted GenAI trace
(invoke_agent root, execute_tool / nested invoke_agent children, conversation id).
Assembly is pure (no OTel needed); emission is injected."""
from __future__ import annotations

from conftest import NS
from weave_agent_adapter.core.model import WireEvent
from weave_agent_adapter.profile import load_profile
from weave_agent_adapter.sinks.genai import GenAISink
from weave_agent_adapter.tracer import Tracer

SID = "s1"


def run_genai(events):
    turns = []
    sink = GenAISink("ent/proj", emit=turns.append)
    tr = Tracer(load_profile("claude-code"), "ent/proj", sink)
    for i, (name, payload) in enumerate(events):
        tr.handle(WireEvent(1, "claude-code", name, 1000.0 + i, payload, 1))
    return turns


def test_turn_emitted_as_invoke_agent_trace():
    turns = run_genai([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "add tests"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "pytest"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"exit": 0}}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done, 3 tests added"}),
    ])
    assert len(turns) == 1
    t = turns[0]
    assert t["name"] == "invoke_agent claude-code"          # harness from session attrs
    a = t["attributes"]
    assert a["gen_ai.operation.name"] == "invoke_agent"
    assert a["gen_ai.conversation.id"]                       # falls back to session/trace id
    assert a["gen_ai.prompt.0.content"] == "add tests"
    assert a["gen_ai.completion.0.content"] == "done, 3 tests added"
    tools = [c for c in t["children"] if c["name"].startswith("execute_tool")]
    assert len(tools) == 1
    ta = tools[0]["attributes"]
    assert ta["gen_ai.tool.name"] == "Bash"
    assert "pytest" in ta["gen_ai.tool.call.arguments"]
    assert ta["weave.permission.permission_decision"] == "allow"
    assert t["end"] > t["start"]                             # hook-captured timing preserved


def test_subagent_nested_as_invoke_agent_child():
    turns = run_genai([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "l1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "l1",
                         "tool_response": {}}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                        "agent_id": "a1", "agent_type": "Explore"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                         "agent_id": "a1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "a1", "agent_type": "Explore"}),
        ("Stop", {"session_id": SID}),
    ])
    (t,) = turns
    launcher = next(c for c in t["children"] if c["name"] == "execute_tool Agent")
    sub = next(c for c in launcher["children"] if c["name"] == "invoke_agent Explore")
    assert any(g["name"] == "execute_tool Read" for g in sub["children"])


def test_multiple_turns_emit_separate_traces_with_same_conversation():
    turns = run_genai([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "one"}),
        ("Stop", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "two"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    assert len(turns) == 2                                   # one trace per turn (precedent)
    convs = {t["attributes"]["gen_ai.conversation.id"] for t in turns}
    assert len(convs) == 1                                   # stitched by conversation id


def test_buffer_cleared_after_session_end():
    sink = GenAISink("ent/proj", emit=lambda t: None)
    tr = Tracer(load_profile("claude-code"), "ent/proj", sink)
    for i, (name, payload) in enumerate([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ]):
        tr.handle(WireEvent(1, "claude-code", name, 1000.0 + i, payload, 1))
    assert sink._calls == {}                                 # no leak across sessions
