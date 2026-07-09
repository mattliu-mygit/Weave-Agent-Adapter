"""Transcript enrichment: per-LLM-call chat spans with token usage, and the
turn linger that finalizes a conversation's last turn without a session end."""
from __future__ import annotations

import datetime
import json

from conftest import run

SID = "s1"


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _transcript(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def test_chat_spans_from_transcript(tmp_path):
    # turn runs t0+1 .. t0+3 (run() stamps one event per second from t0=1000)
    tp = _transcript(tmp_path, [
        {"type": "user", "timestamp": _iso(1001.0), "uuid": "ROOT"},
        {"type": "assistant", "timestamp": _iso(1001.8), "isSidechain": False,
         "message": {"model": "claude-opus-4-8",
                     "usage": {"input_tokens": 1200, "output_tokens": 80,
                               "cache_read_input_tokens": 900},
                     "stop_reason": "tool_use",
                     "content": [{"type": "text", "text": "I'll check the file first."},
                                 {"type": "tool_use", "name": "Read"}]}},
        {"type": "assistant", "timestamp": _iso(1002.6), "isSidechain": False,
         "message": {"model": "claude-opus-4-8",
                     "usage": {"input_tokens": 1400, "output_tokens": 40},
                     "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "All done."}]}},
        # sidechain (subagent) call: must be excluded
        {"type": "assistant", "timestamp": _iso(1002.7), "isSidechain": True,
         "message": {"model": "claude-haiku", "usage": {"input_tokens": 5},
                     "content": []}},
        # outside the turn window: must be excluded
        {"type": "assistant", "timestamp": _iso(1900.0), "isSidechain": False,
         "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 9},
                     "content": []}},
    ])
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": tp}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p", "transcript_path": tp}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "t1"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    chats = [c for c in node["children"] if c["name"].startswith("chat ")]
    assert len(chats) == 2                               # sidechain + out-of-window excluded
    a = chats[0]["attributes"]
    assert a["gen_ai.usage.input_tokens"] == 1200
    assert a["gen_ai.usage.output_tokens"] == 80
    assert a["gen_ai.usage.cache_read.input_tokens"] == 900
    assert a["gen_ai.response.finish_reasons"] == "tool_use"
    assert a["gen_ai.completion.0.content"] == "I'll check the file first."   # intermediate text
    assert chats[0]["end"] >= chats[0]["start"]
    assert chats[1]["attributes"]["gen_ai.response.finish_reasons"] == "end_turn"


def test_chat_text_is_redacted(tmp_path):
    from weave_agent_adapter.redact import Redactor
    tp = _transcript(tmp_path, [
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"model": "m", "usage": {"input_tokens": 1},
                     "content": [{"type": "text",
                                  "text": "your key is sk-ABCDEFGHIJKLMNOP1234"}]}},
    ])
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": tp}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p", "transcript_path": tp}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], redactor=Redactor())
    (node, _), = turns
    (chat,) = [c for c in node["children"] if c["name"].startswith("chat ")]
    assert "sk-ABCDEF" not in chat["attributes"]["gen_ai.completion.0.content"]


def test_message_id_dedup_prevents_token_inflation(tmp_path):
    # same message.id appears twice (multi-content-block streaming): only one record
    tp = _transcript(tmp_path, [
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"id": "msg_001", "model": "claude-opus-4-8",
                     "usage": {"input_tokens": 1200, "output_tokens": 80},
                     "stop_reason": "tool_use",
                     "content": [{"type": "text", "text": "checking"}]}},
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"id": "msg_001", "model": "claude-opus-4-8",
                     "usage": {"input_tokens": 1200, "output_tokens": 80},
                     "stop_reason": "tool_use",
                     "content": [{"type": "text", "text": "checking"},
                                 {"type": "tool_use", "name": "Read"}]}},
        {"type": "assistant", "timestamp": _iso(1002.0), "isSidechain": False,
         "message": {"id": "msg_002", "model": "claude-opus-4-8",
                     "usage": {"input_tokens": 1400, "output_tokens": 40},
                     "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "done"}]}},
    ])
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": tp}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p", "transcript_path": tp}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    chats = [c for c in node["children"] if c["name"].startswith("chat ")]
    assert len(chats) == 2                               # 2 distinct messages, not 3 rows
    total_in = sum(c["attributes"]["gen_ai.usage.input_tokens"] for c in chats)
    assert total_in == 1200 + 1400                       # no inflation


def test_enricher_survives_non_dict_json_line(tmp_path):
    tp = _transcript(tmp_path, [
        "just a string",
        42,
        {"type": "assistant", "timestamp": _iso(1001.5), "isSidechain": False,
         "message": {"model": "m", "usage": {"input_tokens": 1},
                     "content": [{"type": "text", "text": "ok"}]}},
    ])
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": tp}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p", "transcript_path": tp}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (node, _), = turns
    chats = [c for c in node["children"] if c["name"].startswith("chat ")]
    assert len(chats) == 1                               # only the valid dict row


def test_no_enrich_section_degrades_gracefully(tmp_path):
    # codex profile has no [enrich]: turns emit with no chat spans, no errors
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),
    ], harness="codex")
    tr.sweep(now=10_000.0, ttl=1.0)
    (node, _), = turns
    assert [c for c in node["children"] if c["name"].startswith("chat ")] == []


def test_turn_linger_finalizes_last_turn_without_session_end():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID}),          # turn closed, session still open
    ], t0=1000.0)
    assert turns == []                          # pending
    assert tr.finalize_idle_turns(now=1000.0 + 60, linger=120.0) == 0   # too soon
    assert tr.finalize_idle_turns(now=1000.0 + 200, linger=120.0) == 1  # quiet long enough
    assert len(turns) == 1
    assert SID in tr.sessions                   # session stays open for future turns
    # a later turn in the same session still works and emits independently
    from weave_agent_adapter.core.model import WireEvent
    for i, (name, p) in enumerate([("UserPromptSubmit", {"session_id": SID, "prompt": "again"}),
                                   ("Stop", {"session_id": SID}),
                                   ("SessionEnd", {"session_id": SID})]):
        tr.handle(WireEvent(1, "claude-code", name, 2000.0 + i, p, 1))
    assert len(turns) == 2