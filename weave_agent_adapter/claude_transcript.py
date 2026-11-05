"""Read a Claude Code transcript and turn one turn's rows into session Cases.

Claude Code writes `<session>.jsonl`, and for each subagent run
`<session>/subagents/agent-<id>.jsonl` beside an `agent-<id>.meta.json` whose
`toolUseId` names the Task tool call that started it. A turn runs from a user
prompt row to the next one. One API message can span several assistant rows
that share `message.id`.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

from . import records
from .records import ToolFacts, TurnContext
from .repo import repo_identity

INTERRUPTED = "[Request interrupted by user"


@dataclass
class Transcript:
    path: str
    rows: list
    meta: dict              # the subagent's .meta.json, empty for the main session
    agent_id: str | None    # None for the main session
    cache: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass
class TurnRecords:
    turn_id: str
    index: int              # 1-based among this file's turns
    rows: list              # the prompt row through the row before the next prompt, without copied rows
    end_type: str | None    # "completed", "aborted", or None while the turn may still run


def load(path: str) -> Transcript:
    """Parse every line. Only a torn final line (Claude Code mid-write) is skipped."""
    with open(path, "rb") as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    rows = []
    for i, line in enumerate(lines):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if i != len(lines) - 1:
                raise
            continue
        if isinstance(row, dict):
            rows.append(row)
    meta_path = path[:-len(".jsonl")] + ".meta.json"
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    # A fork's file starts with a copy of its parent's history, parent uuids included, through the
    # Task call that started the fork and that call's result. Those rows are the parent's.
    call = meta.get("toolUseId")
    inherited = next((i for i, row in enumerate(rows) if call and _answers(row, call)), None)
    if inherited is not None:
        rows = rows[inherited + 1:]
    agent_id = None
    if meta or os.path.basename(os.path.dirname(path)) == "subagents":
        agent_id = next((r["agentId"] for r in rows if r.get("agentId")), None) or \
            os.path.basename(path)[len("agent-"):-len(".jsonl")]
    return Transcript(path, rows, meta, agent_id)


def _answers(row: dict, call: str) -> bool:
    content = (row.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == call
        for block in content)


def _subagents_dir(t: Transcript) -> str:
    """The session's subagents directory. Workflow agents live in subdirectories of it."""
    if t.agent_id is None:
        return os.path.join(t.path[:-len(".jsonl")], "subagents")
    path = os.path.dirname(t.path)
    while os.path.basename(path) != "subagents" and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path if os.path.basename(path) == "subagents" else os.path.dirname(t.path)


def _agent_files(t: Transcript, pattern: str) -> list:
    return sorted(glob.glob(os.path.join(_subagents_dir(t), "**", pattern), recursive=True))


def subagent_paths(t: Transcript) -> list:
    """Every subagent file of this session, so a parent turn's job can carry their turns."""
    if t.agent_id is not None:
        return []
    return _agent_files(t, "agent-*.jsonl")


def thread_id(t: Transcript):
    return t.agent_id or next((r["sessionId"] for r in t.rows if r.get("sessionId")), None)


def parent_thread_id(t: Transcript):
    """The agent whose Task call started this one, or the session for a top-level subagent."""
    if t.agent_id is None:
        return None
    if t.meta.get("parentAgentId"):
        return t.meta["parentAgentId"]
    call = t.meta.get("toolUseId")
    for path in _agent_files(t, "agent-*.jsonl"):
        if path == t.path or not call:
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            if f'"{call}"' in fh.read():
                return load(path).agent_id
    return next((r["sessionId"] for r in t.rows if r.get("sessionId")), None)


def _content(row: dict):
    return (row.get("message") or {}).get("content")


def _text(content) -> str | None:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip() or None
    return None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _counts(t: Transcript, row: dict) -> bool:
    """A row of this thread. The main file can carry sidechain rows that belong to subagents."""
    return t.agent_id is not None or not row.get("isSidechain")


def _is_typed(t: Transcript, row: dict) -> bool:
    """A message a person or SDK caller sent, not a tool result or a system notification."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary") or not _counts(t, row):
        return False
    # Claude Desktop marks a typed prompt `origin.kind: human`; other origins are notifications.
    origin = (row.get("origin") or {}).get("kind")
    if row.get("promptSource") == "system" or origin not in (None, "human"):
        return False
    # The app's own messages, such as linked-session status, carry `turnOrigin: sdk` and no human
    # origin. A headless SDK caller's prompt looks the same but has another entrypoint.
    if row.get("entrypoint") == "claude-desktop" and row.get("turnOrigin") == "sdk" and origin is None:
        return False
    content = _content(row)
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = _text(content)
    return bool(text) and not text.startswith(INTERRUPTED)


def _end(t: Transcript, rows: list) -> str | None:
    if any(r.get("type") == "user" and (_text(_content(r)) or "").startswith(INTERRUPTED) for r in rows):
        return "aborted"
    replies = [r for r in rows if r.get("type") == "assistant" and _counts(t, r)]
    if replies and (replies[-1].get("message") or {}).get("stop_reason") == "end_turn":
        return "completed"
    return None


def _starts(t: Transcript) -> list:
    """Each turn's first typed message. A typed message with the running turn's `turnIndex` steers
    it. `promptIndex` counts prompts across the session, so it can't mark a steer.

    Cached, because `launch_sites` selects every turn and a large transcript has 100,000s of rows."""
    if "starts" in t.cache:
        return t.cache["starts"]
    starts, running = [], None
    for i, row in enumerate(t.rows):
        if not _is_typed(t, row):
            continue
        turn_index = (row.get("turnPosition") or {}).get("turnIndex")
        if starts and turn_index is not None and turn_index == running:
            continue
        starts.append(i)
        running = turn_index
    if t.agent_id is not None and t.rows and starts[:1] != [0]:
        starts.insert(0, 0)       # a fork starts working without a prompt of its own
    t.cache["starts"] = starts
    return starts


def _copies(t: Transcript) -> set:
    """Positions of rows that repeat an earlier row's uuid. When Claude Desktop compacts a session it
    has compacted before, it first appends a copy of rows starting at that earlier compact boundary,
    original uuids and timestamps included. The copy's first rows would otherwise join the last turn
    and, being older, sort before its prompt."""
    if "copies" in t.cache:
        return t.cache["copies"]
    seen, copies = set(), set()
    for i, row in enumerate(t.rows):
        uuid = row.get("uuid")
        if uuid in seen:
            copies.add(i)
        elif uuid:
            seen.add(uuid)
    t.cache["copies"] = copies
    return copies


def _turns(t: Transcript) -> list:
    """(index, first row, end row) of each turn. A copied prompt's turn is left out, since its original
    already is a turn, but still counts toward `index`, so a session's turn numbers keep the order of
    those already written."""
    starts, copies = _starts(t), _copies(t)
    return [(index, i, j) for index, (i, j) in enumerate(zip(starts, [*starts[1:], len(t.rows)]), start=1)
            if i not in copies]


def _own_rows(t: Transcript, i: int, j: int) -> list:
    copies = _copies(t)
    return [row for k, row in enumerate(t.rows[i:j], start=i) if k not in copies]


def turn_ends(t: Transcript) -> list:
    """(turn_id, "completed" | "aborted" | None) for this file's turns, in order."""
    return [(t.rows[i]["uuid"], _end(t, _own_rows(t, i, j))) for _, i, j in _turns(t) if t.rows[i].get("uuid")]


def select_turn(t: Transcript, turn_id: str) -> TurnRecords | None:
    for index, i, j in _turns(t):
        if t.rows[i].get("uuid") == turn_id:
            rows = _own_rows(t, i, j)
            return TurnRecords(turn_id, index, rows, _end(t, rows))
    return None


def _is_model_reply(t: Transcript, row: dict) -> bool:
    """A reply a model wrote. Claude Code writes its own, such as API errors, under model `<synthetic>`."""
    return row.get("type") == "assistant" and _counts(t, row) and (row.get("message") or {}).get("model") != "<synthetic>"


def _context(t: Transcript, turn: TurnRecords) -> dict:
    prompt = turn.rows[0]
    start = next(i for i, row in enumerate(t.rows) if row is prompt)
    # A turn with no model reply, such as /compact, ran under the model of the last reply before it. This misses a
    # /model switch made since that reply; read the switch's output if turns after a switch must name the new model.
    reply = next((r for r in [*turn.rows, *reversed(t.rows[:start])] if _is_model_reply(t, r)), {})
    return {"model": (reply.get("message") or {}).get("model") or t.meta.get("model"),
            "effort": reply.get("effort") if isinstance(reply.get("effort"), str) else None,
            "cwd": prompt.get("cwd"), "repo": repo_identity(prompt.get("cwd")),
            "git_branch": prompt.get("gitBranch") or None}


def session_case(t: Transcript, turn: TurnRecords, ctx: TurnContext) -> dict:
    first = next((r for r in t.rows if r.get("timestamp")), {})
    return records.session_case(ctx, _context(t, turn), records.iso(records.ms(first.get("timestamp"))))


def _decision(facts: ToolFacts | None, result_row: dict | None) -> str | None:
    """Hooks first. Otherwise the transcript: a rule that allowed the call shows no prompt."""
    if facts and facts.permission_requested:
        return facts.permission_decision
    recorded = (result_row or {}).get("permissionDecision")
    if not isinstance(recorded, dict):
        return None
    if recorded.get("decision") == "reject":
        return "not_approved"
    if recorded.get("decision") == "accept" and str(recorded.get("source", "")).startswith("user"):
        return "approved"
    return None


WORKFLOW_ID = re.compile(r"wf_[0-9a-f]{8}-[0-9a-f]{3}")


def _spawned_by(t: Transcript) -> dict:
    """Tool_use id -> the subagents it started. A Task call's subagent names the call in its meta
    file. A Workflow call's subagents live in the workflow directory its result names."""
    if "spawned" in t.cache:
        return t.cache["spawned"]
    workflows = _workflow_calls(t)
    spawned = {}
    for path in _agent_files(t, "agent-*.meta.json"):
        try:
            with open(path, encoding="utf-8") as fh:
                call = json.load(fh).get("toolUseId")
        except (OSError, ValueError):
            continue
        call = call or workflows.get(os.path.basename(os.path.dirname(path)))
        if call:
            try:
                spawned.setdefault(call, []).append(load(path[:-len(".meta.json")] + ".jsonl").agent_id)
            except (OSError, ValueError):
                continue            # a subagent file missing or mid-write must not fail the turn
    t.cache["spawned"] = spawned
    return spawned


def _workflow_calls(t: Transcript) -> dict:
    """Workflow ID -> the Workflow tool_use whose result names it."""
    started, calls = set(), {}
    for row in t.rows:
        content = (row.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Workflow":
                started.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in started:
                found = WORKFLOW_ID.search(json.dumps(block.get("content"), default=str))
                if found:
                    calls.setdefault(found.group(0), block["tool_use_id"])
    return calls


def launch_sites(t: Transcript) -> dict:
    """Subagent ID -> [turn ID, tool_use ID] of the call in this file that started it."""
    spawned = _spawned_by(t)
    sites = {}
    for turn_id, _ in turn_ends(t) if spawned else ():
        for row in select_turn(t, turn_id).rows:
            content = (row.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    for agent in spawned.get(block.get("id"), ()):
                        sites.setdefault(agent, [turn_id, block["id"]])
    return sites


def turn_records(t: Transcript, turn: TurnRecords, facts: dict, ctx: TurnContext, redactor) -> dict:
    """One turn of this file as a job turn for `CesWriter.write_turn`."""
    rows, steps = turn.rows, []
    typed = [(position, row) for position, row in enumerate(rows) if _is_typed(t, row)]
    for number, (position, row) in enumerate(typed):
        started = records.ms(row.get("timestamp"))
        steps.append(records.step("user_message", started, started, position,
                                  {"text": records.cap(_text(_content(row)))[0], "steering": number > 0}, {}))

    messages = {}
    for position, row in enumerate(rows):
        if row.get("type") == "assistant" and _counts(t, row):
            key = (row.get("message") or {}).get("id") or row.get("uuid")
            messages.setdefault(key, (position, []))[1].append(row)
    totals_tokens = dict.fromkeys(records.TOKEN_KINDS[:4], None)
    for position, parts in messages.values():
        blocks = [b for r in parts for b in ((r.get("message") or {}).get("content") or []) if isinstance(b, dict)]
        last = parts[-1].get("message") or {}
        usage = last.get("usage") or {}
        tokens = {"input": _int(usage.get("input_tokens")), "output": _int(usage.get("output_tokens")),
                  "cache_read": _int(usage.get("cache_read_input_tokens")),
                  "cache_write": _int(usage.get("cache_creation_input_tokens"))}
        for k, v in tokens.items():
            if v is not None:
                totals_tokens[k] = (totals_tokens[k] or 0) + v
        stop = last.get("stop_reason")
        uses_tools = any(b.get("type") == "tool_use" for b in blocks)
        steps.append(records.step(
            "assistant_message", records.ms(parts[0].get("timestamp")), records.ms(parts[-1].get("timestamp")),
            position, {},
            {"text": records.cap("\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip() or None)[0],
             "reasoning": records.cap("\n".join(b.get("thinking", "") for b in blocks
                                                if b.get("type") == "thinking").strip() or None)[0],
             "finish_reason": {"tool_use": "tool_calls", "end_turn": "stop"}.get(stop, stop) if stop
             else ("tool_calls" if uses_tools else None),
             **{f"{k}_tokens": v for k, v in tokens.items()}, "reasoning_tokens": None}))

    results = {}
    for row in rows:
        content = _content(row)
        if row.get("type") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = (row, block)
    spawned = _spawned_by(t)
    for position, row in enumerate(rows):
        if row.get("type") != "assistant" or not _counts(t, row):
            continue
        for block in (row.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id = block.get("id")
            result_row, result = results.get(call_id, (None, None))
            text = _text(result.get("content")) if result else None
            if result and text is None and result.get("content") is not None:
                text = json.dumps(result.get("content"), ensure_ascii=False, default=str)
            output, truncated = records.cap(text)
            failed = bool(result and result.get("is_error"))
            began, ended = records.ms(row.get("timestamp")), records.ms((result_row or {}).get("timestamp"))
            steps.append(records.step(
                "tool_call", began, ended, position,
                {"call_id": call_id, "tool_name": block.get("name"), "arguments": records.as_json(block.get("input"))},
                {"status": "failed" if failed else ("completed" if result else "incomplete"),
                 "output": output, "output_truncated": truncated, "error": output if failed else None,
                 "exit_code": None, "duration_ms": ended - began if began is not None and ended is not None else None,
                 "permission_decision": _decision(facts.get(call_id), result_row)},
                spawns=list(spawned.get(call_id, ()))))

    compactions = 0
    for position, row in enumerate(rows):
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            at = records.ms(row.get("timestamp"))
            steps.append(records.step("compaction", at, at, position,
                                      {"trigger": (row.get("compactMetadata") or {}).get("trigger")}, {}))
            compactions += 1

    stamps = [ms for r in rows if (ms := records.ms(r.get("timestamp"))) is not None]
    tools = [s["output"] for s in steps if s["kind"] == "tool_call"]
    totals = {
        "turn_status": turn.end_type or "incomplete",
        "turn_duration_ms": stamps[-1] - stamps[0] if stamps else None,
        **{f"turn_{k}_tokens": v for k, v in totals_tokens.items()}, "turn_reasoning_tokens": None,
        "turn_steering_count": max(0, len(typed) - 1),
        "turn_denial_count": sum(1 for out in tools if out["permission_decision"] == "not_approved"),
        "turn_tool_error_count": sum(1 for out in tools if out["error"] is not None),
        "turn_compaction_count": compactions,
    }
    parent = parent_thread_id(t)
    return records.turn(ctx, turn_id=turn.turn_id, turn_index=turn.index, agent_id=t.agent_id,
                        parent_agent_id=parent if parent and parent != ctx.session_id else None,
                        context=_context(t, turn), steps=steps, totals=totals, redactor=redactor)
