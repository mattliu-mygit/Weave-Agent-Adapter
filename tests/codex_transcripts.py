"""Build synthetic Codex transcripts with the record shapes Codex 0.153 writes.

Each helper returns one JSONL record. Timestamps come from a counter in
milliseconds so step order is explicit in the tests.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

T0_MS = 1_791_313_200_000          # 2026-10-06T19:00:00.000Z


def ts(ms: int) -> str:
    moment = datetime.fromtimestamp((T0_MS + ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{(T0_MS + ms) % 1000:03d}Z"


def _row(ms, kind, payload):
    return {"timestamp": ts(ms), "type": kind, "payload": payload}


def session_meta(ms, thread_id, **extra):
    return _row(ms, "session_meta", {
        "id": thread_id, "session_id": thread_id, "cwd": "/repo", "originator": "Codex Desktop",
        "cli_version": "0.153.4", "model_provider": "openai",
        "base_instructions": {"provenance": "default", "text": "You are Codex."},
        "git": {"commit_hash": "abc123", "branch": "main", "repository_url": "git@github.com:o/r.git"},
        **extra})


def task_started(ms, turn_id):
    return _row(ms, "event_msg", {"type": "task_started", "turn_id": turn_id,
                                  "model_context_window": 258400})


def turn_context(ms, turn_id):
    return _row(ms, "turn_context", {
        "turn_id": turn_id, "cwd": "/repo", "model": "gpt-6", "effort": "high",
        "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"},
        "collaboration_mode": {"mode": "default", "settings": {}}})


def developer(ms, text="<permissions>"):
    return _row(ms, "response_item", {"type": "message", "role": "developer",
                                      "content": [{"type": "input_text", "text": text}]})


def user(ms, text, kinds=("user.text",)):
    return _row(ms, "response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"content_item_kinds": list(kinds)}})


def world_state(ms, full=True):
    return _row(ms, "world_state", {"full": full, "state": {"model": "gpt-6"}})


def assistant(ms, text, phase="final_answer"):
    return _row(ms, "response_item", {"type": "message", "role": "assistant", "phase": phase,
                                      "content": [{"type": "output_text", "text": text}]})


def reasoning(ms, summary):
    return _row(ms, "response_item", {"type": "reasoning", "encrypted_content": "gAAA",
                                      "summary": [{"type": "summary_text", "text": summary}]})


def exec_call(ms, call_id, script):
    return _row(ms, "response_item", {"type": "custom_tool_call", "name": "exec", "status": "completed",
                                      "call_id": call_id, "input": script})


def exec_output(ms, call_id, text):
    return _row(ms, "response_item", {"type": "custom_tool_call_output", "call_id": call_id,
                                      "output": [{"type": "input_text", "text": text}]})


def function_call(ms, call_id, name, namespace, arguments):
    return _row(ms, "response_item", {"type": "function_call", "name": name, "namespace": namespace,
                                      "call_id": call_id, "arguments": json.dumps(arguments)})


def function_output(ms, call_id, output):
    return _row(ms, "response_item", {"type": "function_call_output", "call_id": call_id,
                                      "output": json.dumps(output)})


def agent_message(ms, author, recipient, text):
    return _row(ms, "response_item", {"type": "agent_message", "author": author, "recipient": recipient,
                                      "content": [{"type": "input_text", "text": text}]})


def usage(ms, turn_id, response_id, call, turn, thread):
    def tokens(total):
        return {"input_tokens": total - 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": total}
    return _row(ms, "token_usage_record", {
        "turn_id": turn_id, "response_id": response_id,
        "usage": tokens(call), "turn_token_usage": tokens(turn), "thread_token_usage": tokens(thread)})


def command(start_ms, end_ms, turn_id, op_id, argv, exit_code=0, output="ok"):
    return _row(end_ms, "event_msg", {
        "type": "item_completed", "turn_id": turn_id,
        "started_at_ms": T0_MS + start_ms, "completed_at_ms": T0_MS + end_ms,
        "item": {"type": "CommandExecution", "id": op_id, "command": argv, "cwd": "/repo",
                 "status": "completed" if exit_code == 0 else "failed", "exit_code": exit_code,
                 "aggregated_output": output, "stderr": "",
                 "duration": {"secs": (end_ms - start_ms) // 1000,
                              "nanos": (end_ms - start_ms) % 1000 * 1_000_000}}})


def collab_op(start_ms, end_ms, turn_id, call_id):
    return _row(end_ms, "event_msg", {
        "type": "item_completed", "turn_id": turn_id,
        "started_at_ms": T0_MS + start_ms, "completed_at_ms": T0_MS + end_ms,
        "item": {"type": "CollabAgentToolCall", "id": call_id, "tool": "wait", "status": "completed",
                 "receiver_thread_ids": [], "agents_states": {}}})


def operation(start_ms, end_ms, turn_id, item):
    return _row(end_ms, "event_msg", {
        "type": "item_completed", "turn_id": turn_id,
        "started_at_ms": T0_MS + start_ms, "completed_at_ms": T0_MS + end_ms, "item": item})


def task_complete(ms, turn_id, duration_ms=1000):
    return _row(ms, "event_msg", {"type": "task_complete", "turn_id": turn_id,
                                  "duration_ms": duration_ms, "time_to_first_token_ms": 100})


def turn_aborted(ms, turn_id):
    return _row(ms, "event_msg", {"type": "turn_aborted", "turn_id": turn_id,
                                  "reason": "interrupted", "duration_ms": 500})


def write(path, rows) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return str(path)


def exec_turn(thread="T1", turn="U1"):
    """One turn: prompt, one exec call running two commands, a final answer."""
    return [
        session_meta(0, thread),
        task_started(1, turn),
        developer(2),
        user(3, "<environment_context>", kinds=("environments.environment_context",)),
        world_state(4),
        turn_context(5, turn),
        user(6, "list files"),
        reasoning(10, "Need to list."),
        assistant(11, "Listing.", phase="commentary"),
        exec_call(12, "call_A", "tools.exec('ls'); tools.exec('pwd')"),
        usage(13, turn, "resp_1", call=100, turn=100, thread=100),
        command(20, 30, turn, "exec-1", ["/bin/zsh", "-lc", "ls"]),
        command(31, 40, turn, "exec-2", ["/bin/zsh", "-lc", "pwd"], exit_code=1, output="no"),
        exec_output(41, "call_A", "a.txt"),
        world_state(42, full=False),
        assistant(50, "Done: a.txt"),
        usage(51, turn, "resp_2", call=120, turn=220, thread=220),
        task_complete(52, turn),
    ]


def interrupted_then_next(thread="T2"):
    """An interrupted turn (no response to its last input), then a completed turn."""
    return [
        session_meta(0, thread),
        task_started(1, "A1"),
        turn_context(2, "A1"),
        user(3, "write a long poem"),
        developer(4, "<turn_aborted>"),
        turn_aborted(5, "A1"),
        task_started(10, "B1"),
        turn_context(11, "B1"),
        user(12, "hi"),
        assistant(13, "Hi!"),
        usage(14, "B1", "resp_b", call=50, turn=50, thread=50),
        task_complete(15, "B1"),
    ]


def subagent_fork(child="C1", parent="T3", parent_turn="P1", child_turn="K1"):
    """A subagent transcript: its own meta, a copy of the parent's history, then its turn."""
    return [
        session_meta(0, child, parent_thread_id=parent, forked_from_id=parent,
                     agent_nickname="Meitner", agent_path="/root/count", subagent_history_start_ordinal=4),
        session_meta(0, parent),
        task_started(1, parent_turn),
        user(2, "parent prompt"),
        _row(3, "event_msg", {"type": "thread_settings_applied"}),
        task_started(10, child_turn),
        developer(11, "<fork>"),
        agent_message(12, "/root", "/root/count", "count files"),
        assistant(13, "3 files"),
        usage(14, child_turn, "resp_c", call=30, turn=30, thread=30),
        task_complete(15, child_turn),
    ]


def spawn_op(start_ms, end_ms, turn_id, call_id, child):
    return operation(start_ms, end_ms, turn_id, {
        "type": "CollabAgentToolCall", "id": call_id, "tool": "spawn_agent", "status": "completed",
        "receiver_thread_ids": [child], "agents_states": {}})


def compaction(ms, trigger="auto"):
    return _row(ms, "compacted", {"type": "compaction", "trigger": trigger})


def parent_with_subagent(parent="T3", turn="P1", child="C1"):
    """A parent turn that spawns subagent `child` through a spawn_agent call, then answers."""
    return [
        session_meta(0, parent),
        task_started(1, turn),
        turn_context(2, turn),
        user(3, "count files with a helper"),
        function_call(10, "call_S", "spawn_agent", None, {"task": "count files"}),
        usage(11, turn, "resp_p1", call=40, turn=40, thread=40),
        spawn_op(12, 13, turn, "call_S", child),
        function_output(14, "call_S", {"agent": child}),
        assistant(20, "There are 3 files."),
        usage(21, turn, "resp_p2", call=60, turn=100, thread=100),
        task_complete(22, turn),
    ]
