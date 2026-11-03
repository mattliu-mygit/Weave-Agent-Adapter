"""Read a Codex transcript (rollout JSONL) and turn one turn's records into session Cases.

Codex writes one JSON record per line. A turn runs from `task_started` to
`task_complete` or `turn_aborted` with the same `turn_id`. A subagent's
transcript starts with a copy of its parent's history; its own records begin at
the line `session_meta.subagent_history_start_ordinal` names.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import records
from .records import ToolFacts, TurnContext

TURN_ENDS = ("task_complete", "turn_aborted")


@dataclass
class Transcript:
    rows: list
    meta: dict            # this thread's session_meta payload (the first one in the file)
    history_start: int    # first line of this thread's own history


@dataclass
class TurnRecords:
    turn_id: str
    index: int                                  # 1-based among this thread's own turns
    rows: list                                  # task_started through the end event, or to EOF
    started: dict
    end: dict | None                            # task_complete or turn_aborted payload
    end_type: str | None
    context: dict                               # last turn_context inside the turn
    operations: list = field(default_factory=list)  # item_completed payloads for this turn
    parent_turn_id: str | None = None           # subagent forks: the parent turn that spawned it


def load(path: str) -> Transcript:
    """Parse every line. Only a torn final line (Codex mid-write) is skipped."""
    with open(path, "rb") as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    rows = []
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):  # a tear can split a multi-byte character
            if i != len(lines) - 1:
                raise
    meta = next((r.get("payload") or {} for r in rows if r.get("type") == "session_meta"), {})
    start = meta.get("subagent_history_start_ordinal")
    return Transcript(rows, meta, start if isinstance(start, int) and start >= 0 else 0)


def event(row: dict, kind: str):
    """The payload of an `event_msg` row of this kind, else None."""
    if row.get("type") != "event_msg":
        return None
    payload = row.get("payload") or {}
    return payload if payload.get("type") == kind else None


def turn_ends(t: Transcript) -> list:
    """(turn_id, end_type or None) for this thread's own turns, in order."""
    order, ends = [], {}
    for row in t.rows[t.history_start:]:
        started = event(row, "task_started")
        if started and started.get("turn_id") and started["turn_id"] not in ends:
            order.append(started.get("turn_id"))
            ends[started.get("turn_id")] = None
        for kind in TURN_ENDS:
            ended = event(row, kind)
            if ended and ended.get("turn_id") in ends:
                ends[ended["turn_id"]] = kind
    return [(turn_id, ends[turn_id]) for turn_id in order]


def select_turn(t: Transcript, turn_id: str) -> TurnRecords | None:
    """The records of one of this thread's own turns, or None if it has no such turn."""
    own = t.rows[t.history_start:]
    index, start = 0, None
    for i, row in enumerate(own):
        started = event(row, "task_started")
        if started and started.get("turn_id"):
            index += 1
            if started.get("turn_id") == turn_id:
                start = i
                break
    if start is None:
        return None
    end_at, end, end_type = len(own) - 1, None, None
    for i in range(start + 1, len(own)):
        next_start = event(own[i], "task_started")
        if next_start and next_start.get("turn_id") and next_start.get("turn_id") != turn_id:
            end_at = i - 1          # killed turn: its rows stop before the next turn begins
            break
        for kind in TURN_ENDS:
            ended = event(own[i], kind)
            if ended and ended.get("turn_id") == turn_id:
                end_at, end, end_type = i, ended, kind
                break
        if end is not None:
            break
    rows = own[start:end_at + 1]
    context = {}
    for row in rows:
        if row.get("type") == "turn_context":
            context = row.get("payload") or {}
    # Operations can complete after the turn's end record, so match by turn_id.
    operations = [
        payload for row in own
        if (payload := event(row, "item_completed")) and payload.get("turn_id") == turn_id
    ]
    parent = None
    for row in t.rows[:t.history_start]:
        started = event(row, "task_started")
        if started:
            parent = started.get("turn_id")
    return TurnRecords(turn_id, index, rows, own[start]["payload"], end, end_type,
                       context, operations, parent)


USAGE_KEYS = ("input", "cached_input", "cache_write_input", "output", "reasoning_output", "total")
OPERATION_TYPES = {
    "CommandExecution", "FileChange", "McpToolCall", "Extension", "ImageView", "CollabAgentToolCall",
}


def _text(value) -> str | None:
    """Text of a message content list, a tool output, or a plain string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(i, dict) and "text" in i for i in value):
        return "\n".join(str(i["text"]) for i in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _prompt_texts(payload: dict) -> list:
    """User-typed text only. Codex also sends AGENTS.md and environment context as user messages."""
    content = payload.get("content") or []
    kinds = (payload.get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds")
    texts = []
    for i, item in enumerate(content):
        if not isinstance(item, dict) or "text" not in item:
            continue
        if isinstance(kinds, list) and (i >= len(kinds) or kinds[i] != "user.text"):
            continue
        texts.append(str(item["text"]))
    return texts


def _usage(record, key) -> dict:
    usage = (record or {}).get(key) or {}
    return {k: _int(usage.get(f"{k}_tokens")) for k in USAGE_KEYS}


def _tokens(usage: dict, prefix: str = "") -> dict:
    return {f"{prefix}input_tokens": usage["input"], f"{prefix}output_tokens": usage["output"],
            f"{prefix}cache_read_tokens": usage["cached_input"],
            f"{prefix}cache_write_tokens": usage["cache_write_input"],
            f"{prefix}reasoning_tokens": usage["reasoning_output"]}


def _is_output(payload: dict) -> bool:
    kind = payload.get("type")
    return kind in ("reasoning", "function_call", "custom_tool_call") or (
        kind == "message" and payload.get("role") == "assistant")


def _segments(rows: list) -> list:
    """Split a turn into model calls: items up to and including each token_usage_record.

    A trailing segment with no usage record is a call that never got a response."""
    segments, current = [], None
    for position, row in enumerate(rows):
        kind = row.get("type")
        if kind == "response_item":
            if current is None:
                current = {"items": [], "usage": None}
            current["items"].append((position, row))
        elif kind == "token_usage_record" and current is not None:
            current["usage"] = row
            segments.append(current)
            current = None
    if current is not None:
        segments.append(current)
    return segments


def _failed(item: dict) -> bool:
    result = item.get("result")
    return item.get("status") == "failed" or _int(item.get("exit_code")) not in (None, 0) or (
        isinstance(result, dict) and result.get("isError") is True)


def _raw_output(item: dict):
    return {"CommandExecution": item.get("aggregated_output"), "FileChange": item.get("stdout"),
            "McpToolCall": item.get("result"), "Extension": item.get("results"),
            "CollabAgentToolCall": item.get("agents_states")}.get(item.get("type"))


def _spawns(item: dict) -> list:
    """Threads a CollabAgentToolCall started. A wait or message to an agent names it too."""
    tool = item.get("tool")
    if item.get("type") != "CollabAgentToolCall" or (tool and not str(tool).startswith("spawn")):
        return []
    return [str(t) for t in item.get("receiver_thread_ids") or [] if t]


def _context(t: Transcript, turn: TurnRecords) -> dict:
    return {"model": _str(turn.context.get("model")), "effort": _str(turn.context.get("effort")),
            "cwd": _str(turn.context.get("cwd")) or _str(t.meta.get("cwd")),
            "git_branch": _str((t.meta.get("git") or {}).get("branch"))}


def session_case(t: Transcript, turn: TurnRecords, ctx: TurnContext) -> dict:
    """The session Case, from the first turn sent. The writer keeps its first body."""
    context = _context(t, turn)
    first = next((r for r in t.rows if r.get("type") == "session_meta"), {})
    return records.session_case(ctx, cwd=context["cwd"], model=context["model"],
                                started_at=records.iso(records.ms(first.get("timestamp"))))


def turn_records(t: Transcript, turn: TurnRecords, facts: dict, ctx: TurnContext, redactor) -> dict:
    """One turn of this thread as a job turn for `CesWriter.write_turn`."""
    meta, rows = t.meta, turn.rows
    parent_thread = _str(meta.get("parent_thread_id"))
    agent_id = _str(meta.get("id")) if parent_thread else None
    steps = []

    users = 0
    for position, row in enumerate(rows):
        payload = row.get("payload") or {}
        if row.get("type") != "response_item":
            continue
        texts = []
        if payload.get("type") == "message" and payload.get("role") == "user":
            texts = _prompt_texts(payload)
        elif (payload.get("type") == "agent_message" and agent_id
              and payload.get("recipient") == meta.get("agent_path")):
            texts = [_text([c for c in payload.get("content") or [] if isinstance(c, dict) and "text" in c])]
        if texts:
            started = records.ms(row.get("timestamp"))
            steps.append(records.step("user_message", started, started, position,
                                      {"text": "\n\n".join(texts), "steering": users > 0}, {}))
            users += 1

    for segment in _segments(rows):
        produced = [(p, r) for p, r in segment["items"] if _is_output(r.get("payload") or {})]
        if not produced:
            continue
        payloads = [r.get("payload") or {} for _, r in produced]
        texts = [_text(p.get("content")) for p in payloads if p.get("type") == "message"]
        summaries = [s.get("text") for p in payloads if p.get("type") == "reasoning"
                     for s in p.get("summary") or [] if isinstance(s, dict) and s.get("text")]
        requested = any(p.get("type") in ("function_call", "custom_tool_call") for p in payloads)
        usage_row = segment["usage"]
        text, _ = records.cap("\n".join(x for x in texts if x) or None)
        ended = records.ms((usage_row or {}).get("timestamp")) or records.ms(produced[-1][1].get("timestamp"))
        steps.append(records.step(
            "assistant_message", records.ms(produced[0][1].get("timestamp")), ended, produced[0][0], {},
            {"text": text, "reasoning": "\n\n".join(summaries) or None,
             "finish_reason": "tool_calls" if requested else ("stop" if usage_row else None),
             **_tokens(_usage((usage_row or {}).get("payload"), "usage"))}))

    calls, outputs = {}, {}
    for position, row in enumerate(rows):
        payload = row.get("payload") or {}
        if row.get("type") != "response_item":
            continue
        if payload.get("type") in ("function_call", "custom_tool_call"):
            calls[payload.get("call_id")] = (position, row)
        elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            outputs[payload.get("call_id")] = (position, row)
    windows = [(call_id, records.ms(row.get("timestamp")),
                records.ms((outputs.get(call_id, (None, {}))[1] or {}).get("timestamp")))
               for call_id, (_, row) in calls.items()]
    operations, compactions = {}, []
    for number, payload in enumerate(turn.operations):
        item = payload.get("item") or {}
        started = _int(payload.get("started_at_ms"))
        if item.get("type") == "ContextCompaction":
            compactions.append((started, _int(payload.get("completed_at_ms")), len(rows) + number,
                                _str(item.get("trigger"))))
            continue
        if item.get("type") not in OPERATION_TYPES:
            continue
        # An operation inside several overlapping calls belongs to the one opened last.
        containing = [(lo, cid) for cid, lo, hi in windows if lo is not None and started is not None
                      and lo <= started and (hi is None or started <= hi)]
        parent = item.get("id") if item.get("id") in calls else (
            max(containing, key=lambda w: w[0])[1] if containing else None)
        if parent is not None:
            operations.setdefault(parent, []).append(item)

    for call_id, (position, row) in calls.items():
        payload = row.get("payload") or {}
        out_row = outputs.get(call_id, (None, None))[1]
        output, truncated = records.cap(_text(((out_row or {}).get("payload") or {}).get("output")))
        custom = payload.get("type") == "custom_tool_call"
        started, ended = records.ms(row.get("timestamp")), records.ms((out_row or {}).get("timestamp"))
        items = operations.get(call_id, [])
        codes = [code for item in items if (code := _int(item.get("exit_code"))) is not None]
        failed = [item for item in items if _failed(item)]
        error = None
        if failed:
            error, _ = records.cap(_str(failed[0].get("stderr")) or _text(_raw_output(failed[0]))
                                   or _str(failed[0].get("status")))
        decisions = [(facts.get(i) or ToolFacts()).permission_decision
                     for i in (call_id, *(item.get("id") for item in items))]
        decision = ("not_approved" if "not_approved" in decisions
                    else "approved" if "approved" in decisions else None)
        steps.append(records.step(
            "tool_call", started, ended, position,
            {"call_id": call_id, "tool_name": _str(payload.get("name")),
             "arguments": records.as_json(payload.get("input") if custom else payload.get("arguments"))},
            {"status": (_str(payload.get("status")) if custom else None) or ("completed" if out_row else "incomplete"),
             "output": output, "output_truncated": truncated, "error": error,
             "exit_code": next((c for c in codes if c != 0), codes[-1] if codes else None),
             "duration_ms": ended - started if started is not None and ended is not None else None,
             "permission_decision": decision},
            spawns=[thread for item in items for thread in _spawns(item)]))

    for position, row in enumerate(rows):
        payload = row.get("payload") or {}
        if payload.get("type") == "compaction":
            at = records.ms(row.get("timestamp"))
            compactions.append((at, at, position, _str(payload.get("trigger"))))
    for started, ended, position, trigger in compactions:
        steps.append(records.step("compaction", started, ended, position, {"trigger": trigger}, {}))

    usage_rows = [r for r in rows if r.get("type") == "token_usage_record"]
    tools = [s["output"] for s in steps if s["kind"] == "tool_call"]
    totals = {
        "turn_status": {"task_complete": "completed", "turn_aborted": "aborted"}.get(turn.end_type, "incomplete"),
        "turn_duration_ms": _int((turn.end or {}).get("duration_ms")),
        **_tokens(_usage(usage_rows[-1].get("payload") if usage_rows else None, "turn_token_usage"), "turn_"),
        "turn_steering_count": max(0, users - 1),
        "turn_denial_count": sum(1 for out in tools if out["permission_decision"] == "not_approved"),
        "turn_tool_error_count": sum(1 for out in tools if out["error"] is not None),
        "turn_compaction_count": len(compactions),
    }
    return records.turn(ctx, turn_id=turn.turn_id, turn_index=turn.index, agent_id=agent_id,
                        parent_agent_id=parent_thread if parent_thread and parent_thread != ctx.session_id else None,
                        context=_context(t, turn), steps=steps, totals=totals, redactor=redactor)


def thread_id(t: Transcript):
    return _str(t.meta.get("id"))


def parent_thread_id(t: Transcript):
    return _str(t.meta.get("parent_thread_id"))


def subagent_paths(t: Transcript) -> list:
    """Codex names subagent transcripts in SubagentStop events, not beside the session file."""
    return []
