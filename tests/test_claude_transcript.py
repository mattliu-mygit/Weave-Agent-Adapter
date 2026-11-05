"""Claude Code transcripts to turn records: turns, merges, tool results, subagents, and interruptions."""
from __future__ import annotations

import json
import os

from weave_agent_adapter import claude_transcript as ct_reader, records
from weave_agent_adapter.records import ToolFacts, TurnContext
import claude_transcripts as ct

TYPES = {"string": str, "integer": int, "boolean": bool, "json": (dict, list)}


class _Plain:
    def scrub(self, value):
        return value


def _ctx():
    return TurnContext("claude-code", ct.SESSION, "default", "agent_session", "1.2.3", None)


def _records(path, turn_id, facts=None):
    t = ct_reader.load(path)
    return ct_reader.turn_records(t, ct_reader.select_turn(t, turn_id), facts or {}, _ctx(), _Plain())


def _subagent(session, agent):
    return os.path.join(session[:-len(".jsonl")], "subagents", f"agent-{agent}.jsonl")


def test_turns_start_at_prompts_and_not_at_tool_results_meta_rows_or_interruptions(tmp_path):
    t = ct_reader.load(ct.session_with_task(tmp_path))

    assert ct_reader.turn_ends(t) == [("u1", "completed"), ("u2", "aborted")]
    assert ct_reader.select_turn(t, "u2").index == 2


def test_rows_of_one_api_message_merge_into_one_assistant_message(tmp_path):
    turn = _records(ct.session_with_task(tmp_path), "u1")

    answers = [c for c in turn["cases"] if c["input"]["kind"] == "assistant_message"]
    assert len(answers) == 2
    first = answers[0]["output"]
    assert (first["text"], first["reasoning"], first["finish_reason"]) == ("Asking a helper.", "Delegate it.", "tool_calls")
    assert (first["input_tokens"], first["output_tokens"], first["cache_read_tokens"], first["cache_write_tokens"]) == (
        100, 10, 50, 5)
    assert answers[1]["output"]["finish_reason"] == "stop"


def test_a_tool_use_pairs_with_its_result(tmp_path):
    session = ct.session_with_task(tmp_path)
    task = next(c for c in _records(session, "u1")["cases"] if c["input"]["kind"] == "tool_call")
    denied = next(c for c in _records(session, "u2")["cases"] if c["input"]["kind"] == "tool_call")

    assert (task["input"]["tool_name"], task["input"]["arguments"]) == ("Task", {"prompt": "count"})
    assert {k: task["output"][k] for k in ("status", "output", "error", "duration_ms", "permission_decision")} == {
        "status": "completed", "output": "3 files", "error": None, "duration_ms": 6, "permission_decision": None}
    assert {k: denied["output"][k] for k in ("status", "error", "permission_decision")} == {
        "status": "failed", "error": "permission denied", "permission_decision": "not_approved"}


def test_hook_facts_win_over_the_transcript_permission(tmp_path):
    turn = _records(ct.session_with_task(tmp_path), "u1",
                    facts={"toolu_task": ToolFacts("Task", permission_requested=True, completed=True)})

    task = next(c for c in turn["cases"] if c["input"]["kind"] == "tool_call")
    assert task["output"]["permission_decision"] == "approved"


def test_an_interrupted_turn_reads_aborted_and_a_compaction_is_a_case(tmp_path):
    session = ct.session_with_task(tmp_path)
    second = _records(session, "u2")
    first = _records(session, "u1")

    assert second["cases"][0]["output"]["turn_status"] == "aborted"
    assert second["cases"][0]["output"]["turn_denial_count"] == 1
    compactions = [c for c in first["cases"] if c["input"]["kind"] == "compaction"]
    assert [c["input"]["trigger"] for c in compactions] == ["auto"]
    assert first["cases"][0]["output"]["turn_compaction_count"] == 1
    assert first["cases"][0]["output"]["turn_input_tokens"] == 300


def test_nested_subagents_name_their_task_calls_and_parents(tmp_path):
    session = ct.session_with_task(tmp_path)
    main = _records(session, "u1")
    a1 = _records(_subagent(session, "a1"), "s1")
    a2 = _records(_subagent(session, "a2"), "n1")

    assert main["cases"][main["spawns"]["a1"]]["input"]["call_id"] == "toolu_task"
    assert (a1["agent_id"], a1["parent_agent_id"]) == ("a1", None)
    assert a1["cases"][a1["spawns"]["a2"]]["input"]["call_id"] == "toolu_nested"
    assert (a2["agent_id"], a2["parent_agent_id"]) == ("a2", "a1")
    assert (a1["cases"][0]["input"]["kind"], a1["cases"][0]["input"]["text"]) == ("user_message", "count")
    assert ct_reader.subagent_paths(ct_reader.load(session)) == [_subagent(session, "a1"), _subagent(session, "a2")]


def test_the_session_case_and_each_task_calls_case_carry_repo_branch_and_day(tmp_path, monkeypatch):
    monkeypatch.setattr(ct_reader, "repo_identity", {"/repo": "wandb/core"}.get)
    session = ct.session_with_task(tmp_path)
    t = ct_reader.load(session)
    main, a1 = _records(session, "u1"), _records(_subagent(session, "a1"), "s1")
    roots = [ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx())["input"],
             main["cases"][main["spawns"]["a1"]]["input"], a1["cases"][a1["spawns"]["a2"]]["input"]]

    expected = {"repo": "wandb/core", "git_branch": "main", "day": "2026-10-06"}
    assert [{k: root[k] for k in expected} for root in roots] == [expected] * 3
    # A retry builds the same bodies, so CES answers it under the same Idempotency-Keys.
    assert ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx())["input"] == roots[0]
    assert _records(session, "u1") == main


def test_every_case_emits_exactly_its_declared_keys_and_types(tmp_path):
    session = ct.session_with_task(tmp_path)
    turns = [_records(session, "u1"), _records(session, "u2"), _records(_subagent(session, "a1"), "s1")]

    for case in (c for turn in turns for c in turn["cases"]):
        kind = case["input"]["kind"]
        assert set(case["input"]) == {*records.COMMON_INPUT, *records.MEMBER_INPUT, *records.KIND_INPUT[kind]}
        totals = records.TURN_TOTALS if case["input"]["step_index"] == 0 else {}
        assert set(case["output"]) == {*records.KIND_OUTPUT[kind], *totals}
        for values, declared in ((case["input"], records.INPUT_FIELDS), (case["output"], records.OUTPUT_FIELDS)):
            for name, value in values.items():
                if value is not None:
                    expected = TYPES[declared[name]]
                    assert isinstance(value, expected) and not (expected is int and isinstance(value, bool)), name
    t = ct_reader.load(session)
    session_case = ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx())
    assert set(session_case["input"]) == {*records.COMMON_INPUT, *records.SESSION_INPUT}
    assert (session_case["input"]["cwd"], session_case["input"]["model"]) == ("/repo", "claude-test")


def test_a_turn_with_no_model_reply_takes_model_and_effort_from_the_last_model_run(tmp_path):
    no_response = ct.assistant(4, "a-2", "msg_2", [ct.text("No response requested.")])
    no_response["message"]["model"] = "<synthetic>"
    del no_response["effort"]
    path = ct.write(tmp_path / "S1.jsonl", [
        ct.prompt(1, "u1", "first"),
        ct.assistant(2, "a-1", "msg_1", [ct.text("ok")], stop_reason="end_turn"),
        ct.prompt(3, "u2", "second"),
        no_response,
        ct.meta_row(5, "m1"),
        ct.prompt(6, "c1", "<command-name>/compact</command-name>"),
    ])

    for turn_id in ("u2", "c1"):
        first = _records(path, turn_id)["cases"][0]["input"]
        assert (first["model"], first["effort"]) == ("claude-test", "high"), turn_id


def test_a_system_notification_is_not_a_turn_and_a_later_typed_message_steers(tmp_path):
    path = ct.write(tmp_path / "S1.jsonl", [
        {**ct.prompt(1, "u1", "first"), "turnPosition": {"promptIndex": 0, "turnIndex": 1}},
        {**ct.prompt(2, "n1", "<task-notification>done</task-notification>"),
         "promptSource": "system", "origin": {"kind": "task-notification"}},
        {**ct.prompt(3, "u1b", "also this"), "turnPosition": {"promptIndex": 1, "turnIndex": 1}},
        ct.assistant(4, "a-1", "msg_1", [ct.text("ok")], stop_reason="end_turn"),
    ])
    t = ct_reader.load(path)

    assert ct_reader.turn_ends(t) == [("u1", "completed")]
    turn = _records(path, "u1")
    users = [c["input"] for c in turn["cases"] if c["input"]["kind"] == "user_message"]
    assert [(u["text"], u["steering"]) for u in users] == [("first", False), ("also this", True)]
    assert turn["cases"][0]["output"]["turn_steering_count"] == 1


def test_claude_desktop_prompts_each_start_a_turn(tmp_path):
    """Claude Desktop marks typed prompts `origin.kind: human`, and `promptIndex` counts prompts
    across the session, so neither makes a prompt a notification or a steer."""
    def typed(n, uuid, text, prompt_index, turn_index):
        return {**ct.prompt(n, uuid, text), "origin": {"kind": "human"}, "promptSource": "sdk",
                "turnPosition": {"promptIndex": prompt_index, "turnIndex": turn_index}}

    path = ct.write(tmp_path / "S1.jsonl", [
        typed(1, "u1", "Review this stack of PRs", 1, 1),
        ct.assistant(2, "a-1", "msg_1", [ct.text("reviewed")], stop_reason="end_turn"),
        {**ct.prompt(3, "n1", "<task-notification>done</task-notification>"),
         "promptSource": "sdk", "origin": {"kind": "task-notification"},
         "turnPosition": {"promptIndex": 1, "turnIndex": 2}},
        typed(4, "u2", "lets go PR by PR", 2, 6),
        ct.assistant(5, "a-2", "msg_2", [ct.text("ok")], stop_reason="end_turn"),
        # The app's own message, such as linked-session status, is not a person's prompt.
        {**ct.prompt(6, "s1", "Linked sessions now: top is idle"), "entrypoint": "claude-desktop",
         "turnOrigin": "sdk", "promptSource": "sdk", "turnPosition": {"promptIndex": 2, "turnIndex": 7}},
    ])

    assert ct_reader.turn_ends(ct_reader.load(path)) == [("u1", "completed"), ("u2", "completed")]


def test_turn_starts_are_found_with_one_pass_over_the_rows(tmp_path, monkeypatch):
    rows = []
    for n in range(10):
        rows += [ct.prompt(2 * n + 1, f"u{n}", f"prompt {n}"),
                 ct.assistant(2 * n + 2, f"a-{n}", f"msg_{n}", [ct.text("ok")], stop_reason="end_turn")]
    t = ct_reader.load(ct.write(tmp_path / "S1.jsonl", rows))
    checked = []
    is_typed = ct_reader._is_typed
    monkeypatch.setattr(ct_reader, "_is_typed", lambda t, row: checked.append(row) or is_typed(t, row))

    for turn_id, _ in ct_reader.turn_ends(t):
        ct_reader.select_turn(t, turn_id)

    assert len(checked) == len(rows)


def test_a_fork_drops_its_copy_of_the_parents_history(tmp_path):
    import json as _json

    from weave_agent_adapter import claude_transcript as reader
    import claude_transcripts as cl

    subagents = tmp_path / "s1" / "subagents"
    subagents.mkdir(parents=True)
    parent = [
        cl.prompt(1000, "p1", "split the work", agent="A"),
        cl.assistant(2000, "p2", "m1", [cl.tool_use("toolu_F", "Task", {"prompt": "fork"})], agent="A"),
    ]
    cl.write(subagents / "agent-A.jsonl", parent)
    fork_rows = [
        *[{**row, "agentId": "F"} for row in parent],
        cl.tool_result(2500, "p3", "toolu_F", "Fork started", agent="F"),
        cl.assistant(3000, "f1", "m2", [cl.text("forked work")], stop_reason="end_turn", agent="F"),
    ]
    cl.write(subagents / "agent-F.jsonl", fork_rows)
    (subagents / "agent-F.meta.json").write_text(
        _json.dumps({"isFork": True, "toolUseId": "toolu_F", "parentAgentId": "A"})
    )

    fork = reader.load(str(subagents / "agent-F.jsonl"))

    assert [row["uuid"] for row in fork.rows] == ["f1"]
    assert reader.turn_ends(fork) == [("f1", "completed")]
    assert reader.parent_thread_id(fork) == "A"


def test_a_workflow_call_starts_the_subagents_in_the_workflow_directory_its_result_names(tmp_path):
    session = ct.write(tmp_path / f"{ct.SESSION}.jsonl", [
        ct.prompt(1, "u1", "run the workflow"),
        ct.assistant(2, "a-1", "msg_1", [ct.tool_use("toolu_wf", "Workflow", {"script": "x"})],
                     stop_reason="tool_use"),
        ct.tool_result(3, "r1", "toolu_wf", "Started workflow wf_0123abcd-4ef"),
        ct.assistant(4, "a-2", "msg_2", [ct.text("Done.")], stop_reason="end_turn"),
    ])
    folder = os.path.join(session[:-len(".jsonl")], "subagents", "workflows", "wf_0123abcd-4ef")
    ct.write(os.path.join(folder, "agent-w1.jsonl"), [
        ct.prompt(1, "w-u1", "review", agent="w1"),
        ct.assistant(2, "w-a1", "msg_w", [ct.text("ok")], stop_reason="end_turn", agent="w1"),
    ])
    with open(os.path.join(folder, "agent-w1.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"agentType": "reviewer", "workflowPhase": "review", "spawnDepth": 1}, fh)

    t = ct_reader.load(session)

    assert ct_reader.launch_sites(t) == {"w1": ["u1", "toolu_wf"]}
