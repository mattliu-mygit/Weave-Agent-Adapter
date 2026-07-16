"""Optional transcript enrichment adds model details without entering the reducer."""
from __future__ import annotations

import datetime
import json

import weave
from weave.conversation import LLM

from conftest import run
from weave_agent_adapter.emit import WeaveTurnEmitter
from weave_agent_adapter.model import WireEvent
from weave_agent_adapter.redact import Redactor

SID = "s1"


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def _transcript(tmp_path, rows):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return str(path)


def _mapped(finalized):
    turn, session = finalized
    return WeaveTurnEmitter(weave_module=weave)._build_turn(turn, session)


def _llms(payload):
    return [span for span in payload["spans"] if isinstance(span, LLM)]


def test_structured_model_activity_from_claude_transcript(tmp_path):
    transcript = _transcript(tmp_path, [
        {"type": "user", "timestamp": _iso(1001.0), "uuid": "ROOT"},
        {"type": "assistant", "timestamp": _iso(1001.8), "isSidechain": False,
         "message": {
             "id": "msg-1",
             "model": "claude-opus-4-8",
             "usage": {"input_tokens": 1200, "output_tokens": 80,
                       "cache_read_input_tokens": 900},
             "stop_reason": "tool_use",
             "content": [
                 {"type": "thinking", "thinking": "I should inspect first."},
                 {"type": "text", "text": "I'll check the file."},
                 {"type": "tool_use", "id": "tool-1", "name": "Read",
                  "input": {"path": "/repo/a.py"}},
             ],
         }},
        {"type": "assistant", "timestamp": _iso(1002.6), "isSidechain": False,
         "message": {
             "id": "msg-2",
             "model": "claude-opus-4-8",
             "usage": {"input_tokens": 1400, "output_tokens": 40},
             "stop_reason": "end_turn",
             "content": [{"type": "text", "text": "All done."}],
         }},
        {"type": "assistant", "timestamp": _iso(1002.7), "isSidechain": True,
         "message": {"model": "claude-haiku", "usage": {"input_tokens": 5},
                     "content": []}},
        {"type": "assistant", "timestamp": _iso(1900.0), "isSidechain": False,
         "message": {"model": "outside", "usage": {"input_tokens": 9},
                     "content": []}},
    ])
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "transcript_path": transcript}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "transcript_path": transcript}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    llms = _llms(_mapped(finalized[0]))

    assert len(llms) == 2
    first = llms[0]
    assert first.provider_name == "anthropic"
    assert first.response_id == "msg-1"
    assert first.response_model == "claude-opus-4-8"
    assert first.usage.input_tokens == 1200
    assert first.usage.output_tokens == 80
    assert first.usage.cache_read_input_tokens == 900
    assert first.finish_reasons == ["tool_use"]
    assert first.reasoning.content == "I should inspect first."
    assert first.output_messages[0].parts[0].content == "I'll check the file."
    tool_part = next(part for part in first.output_messages[0].parts
                     if part.type == "tool_call")
    assert tool_part.id == "tool-1"
    assert tool_part.name == "Read"
    assert json.loads(tool_part.arguments) == {"path": "/repo/a.py"}


def test_enriched_content_is_redacted(tmp_path):
    secret = "sk-ABCDEFGHIJKLMNOP1234"
    transcript = _transcript(tmp_path, [
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {
             "id": "msg-secret",
             "model": "m",
             "usage": {"input_tokens": 1},
             "content": [
                 {"type": "thinking", "thinking": f"inspect {secret}"},
                 {"type": "text", "text": f"found {secret}"},
                 {"type": "tool_use", "id": "tool-secret", "name": "Read",
                  "input": {"api_key": secret}},
             ],
         }},
    ])
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "transcript_path": transcript}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "transcript_path": transcript}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], redactor=Redactor())
    (llm,) = _llms(_mapped(finalized[0]))
    tool_part = next(part for part in llm.output_messages[0].parts
                     if part.type == "tool_call")

    assert secret not in llm.reasoning.content
    assert secret not in llm.output_messages[0].parts[0].content
    assert secret not in tool_part.arguments


def test_message_id_dedup_prevents_token_inflation(tmp_path):
    transcript = _transcript(tmp_path, [
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"id": "msg-1", "model": "m",
                     "usage": {"input_tokens": 1200},
                     "content": [{"type": "text", "text": "checking"}]}},
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"id": "msg-1", "model": "m",
                     "usage": {"input_tokens": 1200},
                     "content": [{"type": "text", "text": "checking"},
                                 {"type": "tool_use", "id": "t", "name": "Read"}]}},
        {"type": "assistant", "timestamp": _iso(1002.0), "isSidechain": False,
         "message": {"id": "msg-2", "model": "m",
                     "usage": {"input_tokens": 1400},
                     "content": [{"type": "text", "text": "done"}]}},
    ])
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "transcript_path": transcript}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "transcript_path": transcript}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    turn, _ = finalized[0]

    assert len(turn.chat_calls) == 2
    assert sum(call["input_tokens"] for call in turn.chat_calls) == 2600


def test_git_branch_comes_from_last_in_window_row(tmp_path):
    transcript = _transcript(tmp_path, [
        {"type": "user", "timestamp": _iso(1001.2), "gitBranch": "main"},
        {"type": "assistant", "timestamp": _iso(1001.8), "isSidechain": False,
         "gitBranch": "feature/x",
         "message": {"model": "m", "usage": {"input_tokens": 1},
                     "content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "timestamp": _iso(1900.0), "gitBranch": "other"},
    ])
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "transcript_path": transcript}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "transcript_path": transcript}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    turn, _ = finalized[0]
    assert turn.git_branch == "feature/x"


def test_malformed_transcript_rows_are_ignored(tmp_path):
    transcript = _transcript(tmp_path, [
        "just a string",
        42,
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"model": "m", "usage": {"input_tokens": 1},
                     "content": [{"type": "text", "text": "ok"}]}},
    ])
    _, finalized = run([
        ("SessionStart", {"session_id": SID, "transcript_path": transcript}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "transcript_path": transcript}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    turn, _ = finalized[0]
    assert len(turn.chat_calls) == 1


def test_profile_without_enricher_keeps_hook_derived_turn():
    tracer, finalized = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),
    ], harness="codex")
    tracer.sweep(now=10_000.0, ttl=1.0)
    turn, _ = finalized[0]
    assert turn.chat_calls == []


def test_turn_linger_finalizes_last_turn_without_session_end():
    tracer, finalized = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),
    ], t0=1000.0)
    assert finalized == []
    assert tracer.finalize_idle_turns(now=1060.0, linger=120.0) == 0
    assert tracer.finalize_idle_turns(now=1200.0, linger=120.0) == 1
    assert len(finalized) == 1
    assert SID in tracer.sessions

    for index, (event, payload) in enumerate([
        ("UserPromptSubmit", {"session_id": SID, "prompt": "again"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ]):
        tracer.handle(WireEvent("claude-code", event, 2000.0 + index, payload))
    assert len(finalized) == 2
