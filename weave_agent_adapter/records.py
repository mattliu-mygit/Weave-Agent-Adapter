"""Harness-neutral session records and the Case field contract.

A session is one session Case, the root of the session Trajectory S, and member
Cases in S: one per user message, assistant message, tool call, and compaction.
A subagent's Cases sit in their own Trajectory, a child of the one that launched it.

CES types a Dataset field from its first value and never retypes it, so every
field name has one declared type across every kind (`INPUT_FIELDS`, `OUTPUT_FIELDS`).
Every Case of a kind carries the same keys, and unavailable values are None.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .model import DEFAULT_TRACE_ROLE

MAX_VALUE_CHARS = 32_000
TRUNCATED = "…[truncated]"

KINDS = ("session", "user_message", "assistant_message", "tool_call", "compaction")
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "reasoning")

COMMON_INPUT = {"kind": "string", "harness": "string", "session_id": "string",
                "trace_role": "string", "adapter_version": "string"}
SESSION_INPUT = {"cwd": "string", "model": "string", "config_version": "string",
                 "started_at": "string"}
MEMBER_INPUT = {
    "session_trajectory_id": "string", "turn_id": "string", "turn_index": "integer",
    "step_index": "integer", "agent_id": "string", "started_at": "string", "ended_at": "string",
    "model": "string", "effort": "string", "cwd": "string", "git_branch": "string",
    "config_version": "string", "permission_mode": "string",
}
KIND_INPUT = {
    "user_message": {"text": "string", "steering": "boolean"},
    "assistant_message": {},
    "tool_call": {"call_id": "string", "tool_name": "string", "arguments": "json"},
    "compaction": {"trigger": "string"},
}
KIND_OUTPUT = {
    "user_message": {},
    "assistant_message": {"text": "string", "reasoning": "string", "finish_reason": "string",
                          **{f"{k}_tokens": "integer" for k in TOKEN_KINDS}},
    "tool_call": {"status": "string", "output": "string", "output_truncated": "boolean",
                  "error": "string", "exit_code": "integer", "duration_ms": "integer",
                  "permission_decision": "string"},
    "compaction": {},
}
# On the turn's first Case (step_index 0), which also takes the turn's Scores.
TURN_TOTALS = {
    "turn_status": "string", "turn_duration_ms": "integer",
    **{f"turn_{k}_tokens": "integer" for k in TOKEN_KINDS},
    **{f"turn_{k}_count": "integer" for k in ("steering", "denial", "tool_error", "compaction")},
}

INPUT_FIELDS = {**COMMON_INPUT, **SESSION_INPUT, **MEMBER_INPUT,
                **{k: v for fields in KIND_INPUT.values() for k, v in fields.items()}}
OUTPUT_FIELDS = {**{k: v for fields in KIND_OUTPUT.values() for k, v in fields.items()}, **TURN_TOTALS}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_KIND_RANK = {"user_message": 0, "assistant_message": 1, "tool_call": 2, "compaction": 3}


@dataclass
class ToolFacts:
    """What hooks reported for one `tool_use_id`."""
    hook_tool_name: str | None = None
    permission_requested: bool = False
    completed: bool = False                     # a PostToolUse arrived

    @property
    def permission_decision(self) -> str | None:
        if not self.permission_requested:
            return None
        return "approved" if self.completed else "not_approved"


@dataclass
class TurnContext:
    """Values the transcript does not hold."""
    harness: str
    session_id: str
    permission_mode: str | None
    trace_role: str = DEFAULT_TRACE_ROLE
    adapter_version: str = ""
    config_version: str | None = None


def fields() -> list:
    """The Dataset field declarations `evaluations setup` sends."""
    return [{"source": source, "name": name, "value_type": value_type}
            for source, declared in (("input", INPUT_FIELDS), ("output", OUTPUT_FIELDS))
            for name, value_type in declared.items()]


def ms(timestamp) -> int | None:
    if not isinstance(timestamp, str):
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None  # one unreadable timestamp must not keep the turn, or the session, from CES
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def iso(milliseconds) -> str | None:
    """RFC 3339 UTC with milliseconds, so string order is time order."""
    if milliseconds is None:
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds % 1000:03d}Z"


def cap(text):
    if text is None or len(text) <= MAX_VALUE_CHARS:
        return text, False
    return text[:MAX_VALUE_CHARS - len(TRUNCATED)] + TRUNCATED, True


def as_json(value):
    """A JSON field never holds a scalar, because CES types it from the first value."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"value": value}
        return parsed if isinstance(parsed, (dict, list)) else {"value": parsed}
    return {"value": value}


def session_case(ctx: TurnContext, *, cwd, model, started_at) -> dict:
    return {"input": {"kind": "session", "harness": ctx.harness, "session_id": ctx.session_id,
                      "trace_role": ctx.trace_role, "adapter_version": ctx.adapter_version,
                      "cwd": cwd, "model": model, "config_version": ctx.config_version,
                      "started_at": started_at},
            "output": {}}


def step(kind, started_ms, ended_ms, position, own_input, own_output, spawns=()) -> dict:
    """One member Case before ordering. `position` breaks ties between Cases that start together.

    `spawns` lists the subagents a tool call launched."""
    return {"kind": kind, "started_ms": started_ms, "ended_ms": ended_ms, "position": position,
            "input": {k: own_input.get(k) for k in KIND_INPUT[kind]},
            "output": {k: own_output.get(k) for k in KIND_OUTPUT[kind]}, "spawns": list(spawns)}


def turn(ctx: TurnContext, *, turn_id, turn_index, agent_id, parent_agent_id, context: dict,
         steps: list, totals: dict, redactor) -> dict:
    """Order the steps and build the turn's Case bodies, ready for `CesWriter.write_turn`.

    `context` holds the turn's model, effort, cwd, and git_branch. The writer fills in
    `session_trajectory_id` and each Case's `trajectory_id`."""
    ordered = sorted(steps, key=lambda s: (s["started_ms"] if s["started_ms"] is not None else 0,
                                           _KIND_RANK[s["kind"]], s["position"]))
    cases, spawns = [], {}
    for index, s in enumerate(ordered):
        case_input = {"kind": s["kind"], "harness": ctx.harness, "session_id": ctx.session_id,
                      "trace_role": ctx.trace_role, "adapter_version": ctx.adapter_version,
                      "session_trajectory_id": None, "turn_id": turn_id, "turn_index": turn_index,
                      "step_index": index, "agent_id": agent_id, "started_at": iso(s["started_ms"]),
                      "ended_at": iso(s["ended_ms"]), "model": context.get("model"),
                      "effort": context.get("effort"), "cwd": context.get("cwd"),
                      "git_branch": context.get("git_branch"), "config_version": ctx.config_version,
                      "permission_mode": ctx.permission_mode, **s["input"]}
        case_output = {**s["output"], **(totals if index == 0 else {})}
        # Scrub values only: names like `input_tokens` would match the redactor's key denylist.
        cases.append({"input": {k: redactor.scrub(v) for k, v in case_input.items()},
                      "output": {k: redactor.scrub(v) for k, v in case_output.items()}})
        for spawned in s["spawns"]:
            spawns.setdefault(spawned, index)
    return {"agent_id": agent_id, "parent_agent_id": parent_agent_id, "turn_id": turn_id,
            "cases": cases, "spawns": spawns}
