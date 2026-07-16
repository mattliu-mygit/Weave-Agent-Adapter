"""Reducer-to-Weave integration through the canonical turn model."""
from __future__ import annotations

import json

import weave
from weave.conversation import SubAgent, Tool

from conftest import CapturingEmitter, run
from weave_agent_adapter.emit import WeaveTurnEmitter
from weave_agent_adapter.model import WireEvent
from weave_agent_adapter.profile import Profile
from weave_agent_adapter.tracer import Tracer

SID = "s1"


def _mapped(finalized):
    turn, session = finalized
    return WeaveTurnEmitter(weave_module=weave)._build_turn(turn, session)


def test_hook_events_become_typed_weave_turn():
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "add tests"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "pytest"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"exit": 0}}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    payload = _mapped(finalized[0])

    assert payload["agent_name"] == "claude-code"
    assert [message.content for message in payload["messages"]] == ["add tests", "done"]
    tool = next(span for span in payload["spans"] if isinstance(span, Tool))
    assert tool.name == "Bash"
    assert tool.tool_call_id == "t1"
    assert json.loads(tool.arguments) == {"command": "pytest"}
    assert json.loads(tool.result)["output"] == {"exit": 0}
    assert payload["started_at"] < payload["ended_at"]


def test_late_subagent_activity_is_included_as_flat_typed_spans():
    _, finalized = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "inspect"}),
        ("Stop", {"session_id": SID}),
        ("SubagentStart", {"session_id": SID, "agent_id": "a1", "agent_type": "Explore"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                        "agent_id": "a1", "agent_type": "Explore"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "i1",
                         "agent_id": "a1", "tool_response": {"content": "ok"}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "a1", "agent_type": "Explore",
                          "last_assistant_message": "found it"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    payload = _mapped(finalized[0])

    subagent = next(span for span in payload["spans"] if isinstance(span, SubAgent))
    tool = next(span for span in payload["spans"] if isinstance(span, Tool))
    assert subagent.name == "Explore"
    assert subagent.agent_id == "a1"
    assert tool.name == "Read"
    assert tool.tool_call_id == "i1"
    assert payload["ended_at"] >= subagent.ended_at


def test_turns_share_conversation_and_keep_filterable_counters():
    _, finalized = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "one"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PermissionDenied", {"session_id": SID, "tool_use_id": "t1",
                              "denial_reason": "no"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "try this instead"}),
        ("Stop", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "two"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    first, second = map(_mapped, finalized)

    assert first["conversation_id"] == second["conversation_id"]
    assert [message.content for message in first["messages"][:2]] == [
        "one", "try this instead",
    ]
    assert first["attributes"]["weave_agent_adapter.steering_count"] == 1
    assert first["attributes"]["weave_agent_adapter.denial_count"] == 1
    assert second["attributes"]["weave_agent_adapter.steering_count"] == 0
    assert second["attributes"]["weave_agent_adapter.denial_count"] == 0


def test_third_party_profile_uses_the_same_mapping():
    profile = Profile(
        name="third-party",
        events={
            "Started": "session_start",
            "Prompt": "turn_start",
            "Finished": "turn_end",
            "Closed": "session_end",
        },
        fields={
            "session_id": "id",
            "prompt": "input",
            "assistant_message": "output",
            "conversation": "conversation_id",
        },
        registration={},
        thread={"source": "field", "id_field": "conversation"},
    )
    finalized = []
    tracer = Tracer(profile, "ent/proj", emitter=CapturingEmitter(finalized))
    for index, (event, payload) in enumerate([
        ("Started", {"id": "s", "conversation_id": "conv"}),
        ("Prompt", {"id": "s", "input": "hello"}),
        ("Finished", {"id": "s", "output": "hi"}),
        ("Closed", {"id": "s"}),
    ]):
        tracer.handle(WireEvent("third-party", event, 1.0 + index, payload))

    mapped = _mapped(finalized[0])
    assert mapped["conversation_id"] == "conv"
    assert mapped["agent_name"] == "third-party"
    assert [message.content for message in mapped["messages"]] == ["hello", "hi"]
