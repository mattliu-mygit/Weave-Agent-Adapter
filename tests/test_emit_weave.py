"""Public Weave Conversation SDK mapping and exporter lifecycle."""
from __future__ import annotations

import json
from datetime import timezone
from types import SimpleNamespace

import weave
from weave.conversation import LLM, Message, SubAgent, Tool

from weave_agent_adapter.emit import WeaveTurnEmitter, serializable_payload
from weave_agent_adapter.model import (
    Decision,
    Permission,
    Session,
    Steering,
    SteeringKind,
    ToolCall,
    ToolStatus,
    Turn,
)


def _turn() -> Turn:
    turn = Turn(started_at=10.0, input_text="build it", output_text="done", ended_at=20.0)
    turn.steering.append(Steering(SteeringKind.INTERJECTION, 12.0, "use spans"))
    turn.compactions.append((15.0, "auto"))
    turn.tool_calls["call-1"] = ToolCall(
        correlation_key="call-1",
        tool_name="Bash",
        tool_input={"command": "pytest"},
        started_at=13.0,
        ended_at=14.0,
        status=ToolStatus.OK,
        output={"exit": 0},
        permission=Permission(decision=Decision.ALLOW),
        agent_id="agent-1",
    )
    turn.subagents["agent-1"] = {
        "agent_id": "agent-1",
        "type": "Explore",
        "started_at": 12.5,
        "ended_at": 18.0,
        "output": "found it",
    }
    turn.chat_calls.append({
        "model": "claude-opus-4-8",
        "provider_name": "anthropic",
        "response_id": "msg-1",
        "response_model": "claude-opus-4-8",
        "started_at": 11.0,
        "ended_at": 12.0,
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_tokens": 6,
        "reasoning_tokens": 2,
        "finish_reason": "tool_use",
        "text": "checking",
        "reasoning": "inspect first",
        "tool_calls": [
            {"id": "call-1", "name": "Bash", "arguments": {"command": "pytest"}},
        ],
    })
    return turn


def _session(project: str = "team/project") -> Session:
    return Session(
        session_id="session-1",
        thread_id="conversation-1",
        project=project,
        harness="claude-code",
        config_version="cfg-1",
        cwd="/repo",
        last_activity=20.0,
    )


def test_builds_public_conversation_sdk_objects():
    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(_turn(), _session())

    assert payload["conversation_id"] == "conversation-1"
    assert [message.role for message in payload["messages"]] == [
        "user", "user", "assistant",
    ]
    assert all(isinstance(message, Message) for message in payload["messages"])
    assert any(isinstance(span, LLM) for span in payload["spans"])
    assert any(isinstance(span, Tool) for span in payload["spans"])
    assert any(isinstance(span, SubAgent) for span in payload["spans"])
    assert payload["started_at"].tzinfo == timezone.utc
    assert payload["attributes"]["weave_agent_adapter.compaction_count"] == 1
    assert payload["attributes"]["weave_agent_adapter.compaction_triggers"] == ["auto"]
    assert payload["attributes"]["weave_agent_adapter.cwd"] == "/repo"


def test_preserves_llm_content_without_inferring_inputs():
    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(_turn(), _session())
    llm = next(span for span in payload["spans"] if isinstance(span, LLM))

    assert llm.provider_name == "anthropic"
    assert llm.response_id == "msg-1"
    assert llm.response_model == "claude-opus-4-8"
    assert llm.input_messages == []
    assert llm.reasoning.content == "inspect first"
    assert llm.usage.input_tokens == 10
    assert llm.usage.output_tokens == 4
    assert llm.usage.cache_read_input_tokens == 6
    assert llm.usage.reasoning_tokens == 2
    assert llm.finish_reasons == ["tool_use"]
    message = llm.output_messages[0]
    assert message.parts[0].content == "checking"
    tool_call = next(part for part in message.parts if part.type == "tool_call")
    assert tool_call.id == "call-1"
    assert tool_call.name == "Bash"
    assert json.loads(tool_call.arguments) == {"command": "pytest"}


def test_subagent_tools_are_flat_typed_siblings():
    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(_turn(), _session())
    subagent = next(span for span in payload["spans"] if isinstance(span, SubAgent))
    tool = next(span for span in payload["spans"] if isinstance(span, Tool))

    assert subagent.name == "Explore"
    assert subagent.agent_id == "agent-1"
    assert tool.name == "Bash"
    assert tool.tool_call_id == "call-1"
    assert json.loads(tool.result)["agent_id"] == "agent-1"
    assert tool.started_at >= payload["started_at"]
    assert tool.ended_at <= payload["ended_at"]


def test_tool_result_preserves_status_permission_and_bounded_error():
    turn = _turn()
    call = turn.tool_calls["call-1"]
    call.status = ToolStatus.ERROR
    call.output = None
    call.error = "x" * 40_000
    call.permission = Permission(decision=Decision.DENY, reason="not allowed")

    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(turn, _session())
    tool = next(span for span in payload["spans"] if isinstance(span, Tool))
    result = json.loads(tool.result)

    assert result["status"] == "error"
    assert result["permission"] == {"decision": "deny", "reason": "not allowed"}
    assert len(result["error"]) < 40_000
    assert result["error"].endswith("…[truncated]")


def test_mapping_is_harness_agnostic():
    turn = _turn()
    turn.model = "root-model"
    turn.permission_mode = "review"
    turn.turn_id = "turn-1"
    turn.chat_calls[0]["provider_name"] = "custom-provider"
    turn.chat_calls[0]["model"] = None
    session = _session()
    session.harness = "my-harness"

    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(turn, session)
    llm = next(span for span in payload["spans"] if isinstance(span, LLM))

    assert payload["agent_name"] == "my-harness"
    assert payload["model"] == "root-model"
    assert payload["attributes"]["weave_agent_adapter.permission_mode"] == "review"
    assert payload["attributes"]["weave_agent_adapter.turn_id"] == "turn-1"
    assert llm.provider_name == "custom-provider"


def test_initializes_once_per_project_and_logs_every_turn(monkeypatch):
    initialized = []
    logged = []
    monkeypatch.setattr(weave, "init", initialized.append)
    monkeypatch.setattr(weave, "log_turn", lambda **payload: logged.append(payload))
    emitter = WeaveTurnEmitter(weave_module=weave)

    assert emitter.emit_turn(_turn(), _session()) is True
    assert emitter.emit_turn(_turn(), _session()) is True
    assert initialized == ["team/project"]
    assert len(logged) == 2


def test_project_change_reinitializes_for_the_new_project(monkeypatch):
    initialized = []
    monkeypatch.setattr(weave, "init", initialized.append)
    monkeypatch.setattr(weave, "log_turn", lambda **payload: None)
    emitter = WeaveTurnEmitter(weave_module=weave)

    emitter.emit_turn(_turn(), _session("team/one"))
    emitter.emit_turn(_turn(), _session("team/two"))
    emitter.emit_turn(_turn(), _session("team/two"))
    assert initialized == ["team/one", "team/two"]


def test_init_failure_retries_the_next_handoff(monkeypatch):
    attempts = []

    def initialize(project):
        attempts.append(project)
        if len(attempts) == 1:
            raise RuntimeError("auth")

    logged = []
    monkeypatch.setattr(weave, "init", initialize)
    monkeypatch.setattr(weave, "log_turn", lambda **payload: logged.append(payload))
    emitter = WeaveTurnEmitter(weave_module=weave)

    assert emitter.emit_turn(_turn(), _session()) is False
    assert emitter.emit_turn(_turn(), _session()) is True
    assert attempts == ["team/project", "team/project"]
    assert len(logged) == 1


def test_failed_log_does_not_block_the_next_turn(monkeypatch):
    calls = []

    def log_turn(**payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("network")

    monkeypatch.setattr(weave, "init", lambda project: None)
    monkeypatch.setattr(weave, "log_turn", log_turn)
    emitter = WeaveTurnEmitter(weave_module=weave)

    assert emitter.emit_turn(_turn(), _session()) is False
    assert emitter.emit_turn(_turn(), _session()) is True
    assert len(calls) == 2


def test_flush_is_bounded_and_reports_false(monkeypatch):
    from opentelemetry import trace

    calls = []
    provider = SimpleNamespace(
        force_flush=lambda timeout_millis: calls.append(timeout_millis) or False,
    )
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)

    assert WeaveTurnEmitter(weave_module=weave).flush() is False
    assert calls == [5_000]


def test_payload_serialization_keeps_sdk_objects_structured():
    payload = WeaveTurnEmitter(weave_module=weave)._build_turn(_turn(), _session())
    serialized = serializable_payload(payload)

    assert serialized["messages"][0]["role"] == "user"
    assert serialized["spans"][0]["started_at"].endswith("Z")
