"""Build synthetic Claude Code transcripts with the row shapes Claude Code writes.

Timestamps come from a millisecond counter so step order is explicit in the tests.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

T0_MS = 1_791_313_200_000          # 2026-10-06T19:00:00.000Z
SESSION = "S1"


def ts(ms: int) -> str:
    moment = datetime.fromtimestamp((T0_MS + ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{(T0_MS + ms) % 1000:03d}Z"


def _row(ms, kind, uuid, agent=None, **extra):
    row = {"type": kind, "uuid": uuid, "timestamp": ts(ms), "sessionId": SESSION, "cwd": "/repo",
           "gitBranch": "main", "isSidechain": agent is not None, **extra}
    if agent:
        row["agentId"] = agent
    return row


def prompt(ms, uuid, text, agent=None):
    return _row(ms, "user", uuid, agent, message={"role": "user", "content": text})


def assistant(ms, uuid, message_id, blocks, stop_reason=None, tokens=(100, 10, 50, 5), agent=None):
    usage = dict(zip(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), tokens))
    return _row(ms, "assistant", uuid, agent, effort="high", message={
        "id": message_id, "role": "assistant", "model": "claude-test", "content": blocks,
        "stop_reason": stop_reason, "usage": usage})


def text(value):
    return {"type": "text", "text": value}


def thinking(value):
    return {"type": "thinking", "thinking": value}


def tool_use(call_id, name, arguments):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def tool_result(ms, uuid, call_id, content, is_error=False, decision=("accept", "config"), agent=None):
    extra = {"permissionDecision": {"decision": decision[0], "source": decision[1]}} if decision else {}
    return _row(ms, "user", uuid, agent, **extra, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}]})


def interrupted(ms, uuid):
    return _row(ms, "user", uuid, message={"role": "user", "content": [text("[Request interrupted by user]")]})


def compact(ms, uuid, trigger="auto"):
    return _row(ms, "system", uuid, subtype="compact_boundary", compactMetadata={"trigger": trigger})


def meta_row(ms, uuid):
    return _row(ms, "user", uuid, isMeta=True, message={"role": "user", "content": "<local-command-caveat>"})


def write(path, rows) -> str:
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return str(path)


def write_subagent(session_path, agent, call_id, depth, rows) -> str:
    folder = os.path.join(str(session_path)[:-len(".jsonl")], "subagents")
    path = write(os.path.join(folder, f"agent-{agent}.jsonl"), rows)
    with open(os.path.join(folder, f"agent-{agent}.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"agentType": "general-purpose", "toolUseId": call_id, "spawnDepth": depth, "finished": True}, fh)
    return path


def session_with_task(tmp_path):
    """Two turns. The first runs a Task subagent `a1` that runs nested subagent `a2`. The second is interrupted."""
    session = write(tmp_path / f"{SESSION}.jsonl", [
        meta_row(0, "m0"),
        prompt(1, "u1", "count the files"),
        assistant(2, "a-1", "msg_1", [thinking("Delegate it.")]),
        assistant(3, "a-2", "msg_1", [text("Asking a helper."), tool_use("toolu_task", "Task", {"prompt": "count"})],
                  stop_reason="tool_use"),
        tool_result(9, "r1", "toolu_task", "3 files"),
        compact(10, "c1"),
        assistant(11, "a-3", "msg_2", [text("There are 3 files.")], stop_reason="end_turn", tokens=(200, 20, 60, 0)),
        prompt(20, "u2", "now list them"),
        assistant(21, "a-4", "msg_3", [tool_use("toolu_ls", "Bash", {"command": "ls"})], stop_reason="tool_use"),
        tool_result(22, "r2", "toolu_ls", "permission denied", is_error=True, decision=("reject", "user_temporary")),
        interrupted(23, "i1"),
    ])
    write_subagent(session, "a1", "toolu_task", 1, [
        prompt(4, "s1", "count", agent="a1"),
        assistant(5, "b-1", "msg_a1", [tool_use("toolu_nested", "Task", {"prompt": "really count"})],
                  stop_reason="tool_use", agent="a1"),
        tool_result(7, "sr1", "toolu_nested", "3", agent="a1"),
        assistant(8, "b-2", "msg_a2", [text("3 files")], stop_reason="end_turn", agent="a1"),
    ])
    write_subagent(session, "a2", "toolu_nested", 2, [
        prompt(5, "n1", "really count", agent="a2"),
        assistant(6, "c-1", "msg_n1", [text("3")], stop_reason="end_turn", agent="a2"),
    ])
    return session
