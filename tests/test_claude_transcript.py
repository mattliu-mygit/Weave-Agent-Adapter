"""Claude Code transcripts to turn records: turns, merges, tool results, subagents, and interruptions."""
from __future__ import annotations

import json
import os

from weave_agent_adapter import claude_transcript as ct_reader, records
from weave_agent_adapter.records import TurnContext
import claude_transcripts as ct

TYPES = {"string": str, "integer": int, "boolean": bool, "json": (dict, list)}


class _Plain:
    def scrub(self, value):
        return value


def _ctx():
    return TurnContext("claude-code", ct.SESSION, "default", "agent_session", "1.2.3", None)


def _records(path, turn_id, decisions=None):
    t = ct_reader.load(path)
    return ct_reader.turn_records(t, ct_reader.select_turn(t, turn_id), decisions or {}, _ctx(), _Plain())


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


def test_hook_decisions_win_over_the_transcript_permission(tmp_path):
    turn = _records(ct.session_with_task(tmp_path), "u1", decisions={"toolu_task": "approved"})

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
    roots = [ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx(), _Plain())["input"],
             main["cases"][main["spawns"]["a1"]]["input"], a1["cases"][a1["spawns"]["a2"]]["input"]]

    expected = {"repo": "wandb/core", "git_branch": "main", "day": "2026-10-06"}
    assert [{k: root[k] for k in expected} for root in roots] == [expected] * 3
    # A retry builds the same bodies, so CES answers it under the same Idempotency-Keys.
    assert ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx(), _Plain())["input"] == roots[0]
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
    session_case = ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx(), _Plain())
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


def _compact(command_ms, n) -> list:
    """The rows a /compact entered at `command_ms` writes: its compact boundary and the summary, stamped
    when the compaction finished, then a meta caveat and the command row, stamped when it was entered."""
    return [ct.compact(command_ms + 5, f"c{n}", trigger="manual"),
            {**ct.prompt(command_ms + 5, f"cs{n}", "This session is being continued..."), "isCompactSummary": True},
            ct.meta_row(command_ms, f"m{n}"),
            ct.prompt(command_ms, f"k{n}", "<command-name>/compact</command-name>")]


def _recompacted(tmp_path, *, copy: bool, auto: bool = False) -> str:
    """Claude Desktop's shape in session d258ec50. Turn u0 runs, then /compact k1, then turns u1 and u2.
    The next /compact, k2, first appends a copy of the rows since the first boundary, in which the last
    reply from before that compaction follows the summary, re-parented. Then come k2's own rows.

    With `auto`, session ae268659's shape: the second compaction is automatic, while u2 runs. After the
    copy come its boundary and summary, then the rest of u2."""
    reply = ct.assistant(2, "a-0", "msg_0", [ct.text("Both screens launched.")], stop_reason="end_turn")
    first, since = _compact(5, 1), [ct.prompt(11, "u1", "continue"),
                                    ct.assistant(12, "a-1", "msg_1", [ct.text("ok")], stop_reason="end_turn")]
    second = [ct.compact(25, "c2"),
              {**ct.prompt(25, "cs2", "This session is being continued..."), "isCompactSummary": True},
              ct.assistant(26, "a-2b", "msg_2b", [ct.text("Gone.")], stop_reason="end_turn")] if auto else _compact(25, 2)
    return ct.write(tmp_path / "S1.jsonl", [
        ct.prompt(1, "u0", "start"),
        reply,
        *first,
        *since,
        ct.prompt(20, "u2", "lets keep going"),
        ct.assistant(21, "a-2", "msg_2", [ct.text("Going.")], stop_reason="end_turn"),
        *([*first[:2], reply, *first[2:], *since] if copy else []),
        *second,
        ct.prompt(31, "u3", "next"),
        ct.assistant(32, "a-3", "msg_3", [ct.text("done")], stop_reason="end_turn"),
    ])


def test_a_turn_starts_at_its_prompt_when_a_later_compaction_copies_older_rows_after_it(tmp_path):
    t = ct_reader.load(_recompacted(tmp_path, copy=True))
    turn = _records(t.path, "u2")

    assert ct_reader.turn_ends(t) == [("u0", "completed"), ("k1", None), ("u1", "completed"),
                                      ("u2", "completed"), ("k2", None), ("u3", "completed")]
    first = turn["cases"][0]
    assert (first["input"]["kind"], first["input"]["text"]) == ("user_message", "lets keep going")
    assert first["input"]["started_at"] == ct.ts(20)
    # Before, the copied reply (2 ms) and boundary (10 ms) sorted ahead of the prompt.
    assert [(c["input"]["kind"], c["input"]["started_at"]) for c in turn["cases"]] == [
        ("user_message", ct.ts(20)), ("assistant_message", ct.ts(21))]
    assert first["output"]["turn_duration_ms"] == 1
    # k2's boundary goes to k2, and its summary and caveat, written after the copy, go in no turn.
    assert [row["uuid"] for row in ct_reader.select_turn(t, "u2").rows] == ["u2", "a-2"]
    # The copied prompts k1 and u1 still count, so k2 and u3 keep the turn numbers earlier versions gave them.
    assert [ct_reader.select_turn(t, u).index for u in ("u0", "k1", "u1", "u2", "k2", "u3")] == [1, 2, 3, 4, 7, 8]


def test_turns_without_copied_rows_build_as_before(tmp_path):
    copied = _recompacted(tmp_path / "copied", copy=True)
    plain = _recompacted(tmp_path / "plain", copy=False)

    for turn_id in ("u0", "k1", "u1", "k2", "u3"):
        expected = _records(plain, turn_id)
        if turn_id in ("k2", "u3"):         # the plain file's turns 5 and 6; the copied prompts add 2
            expected = {**expected, "cases": [{**c, "input": {**c["input"], "turn_index": c["input"]["turn_index"] + 2}}
                                              for c in expected["cases"]]}
        assert _records(copied, turn_id) == expected, turn_id


def test_a_compact_boundary_is_written_once_in_the_turn_of_the_compact_that_wrote_it(tmp_path):
    """Each /compact writes its boundary just before the command row. Without copied rows the boundary
    would fall in the turn before, which its Stop has usually sent already. After copied rows it
    would fall among them, in a turn no one sends."""
    for copy in (False, True):
        t = ct_reader.load(_recompacted(tmp_path / str(copy), copy=copy))
        turns = {turn_id: _records(t.path, turn_id) for turn_id, _ in ct_reader.turn_ends(t)}
        compactions = {turn_id: [c["input"]["started_at"] for c in turn["cases"] if c["input"]["kind"] == "compaction"]
                       for turn_id, turn in turns.items()}

        assert compactions == {"u0": [], "k1": [ct.ts(10)], "u1": [], "u2": [], "k2": [ct.ts(30)], "u3": []}, copy
        for turn_id in ("k1", "k2"):
            cases = turns[turn_id]["cases"]
            assert [(c["input"]["kind"], c["input"].get("text")) for c in cases] == [
                ("user_message", "<command-name>/compact</command-name>"), ("compaction", None)], copy
            # The turn runs from the command until the compaction finished.
            assert (cases[0]["output"]["turn_duration_ms"], cases[0]["output"]["turn_compaction_count"]) == (5, 1), copy


def test_the_rest_of_a_turn_after_an_auto_compaction_that_follows_copied_rows_stays_in_that_turn(tmp_path):
    """Claude Desktop copies older rows before an auto compaction too. The rows after the copy are the
    running turn's, so u2 builds as it does in the same file without the copy."""
    copied = _recompacted(tmp_path / "copied", copy=True, auto=True)
    plain = _recompacted(tmp_path / "plain", copy=False, auto=True)

    assert ct_reader.turn_ends(ct_reader.load(copied)) == [("u0", "completed"), ("k1", None), ("u1", "completed"),
                                                           ("u2", "completed"), ("u3", "completed")]
    u2 = _records(copied, "u2")["cases"]
    assert [(c["input"]["kind"], c["input"]["started_at"]) for c in u2] == [
        ("user_message", ct.ts(20)), ("assistant_message", ct.ts(21)), ("compaction", ct.ts(25)),
        ("assistant_message", ct.ts(26))]
    assert (u2[0]["output"]["turn_duration_ms"], u2[0]["output"]["turn_compaction_count"]) == (6, 1)
    for turn_id in ("u0", "k1", "u1", "u2", "u3"):
        expected = _records(plain, turn_id)
        if turn_id == "u3":                 # the plain file's turn 5; the copied prompts add 2
            expected = {**expected, "cases": [{**c, "input": {**c["input"], "turn_index": 7}} for c in expected["cases"]]}
        assert _records(copied, turn_id) == expected, turn_id


def test_rows_a_copy_brings_back_that_no_earlier_row_has_stay_out_of_the_turn_before_it(tmp_path):
    """Session f9b63d54: rows that failed to write on a full disk appear only in a later copy, stamped
    hours before the turn the copy follows. They are rewritten history, not that turn's work."""
    reply = ct.assistant(2, "a-0", "msg_0", [ct.text("Both screens launched.")], stop_reason="end_turn")
    first = _compact(5, 1)
    lost = ct.assistant(13, "a-1b", "msg_1b", [ct.text("The disk is full.")], stop_reason="end_turn")
    path = ct.write(tmp_path / "S1.jsonl", [
        ct.prompt(1, "u0", "start"), reply, *first,
        ct.prompt(11, "u1", "continue"), ct.assistant(12, "a-1", "msg_1", [ct.text("ok")], stop_reason="end_turn"),
        ct.prompt(20, "u2", "lets keep going"),
        ct.assistant(21, "a-2", "msg_2", [ct.text("Going.")], stop_reason="end_turn"),
        *first[:2], reply, *first[2:], ct.prompt(11, "u1", "continue"), lost,
        *_compact(25, 2),
    ])

    assert [(c["input"]["kind"], c["input"]["started_at"]) for c in _records(path, "u2")["cases"]] == [
        ("user_message", ct.ts(20)), ("assistant_message", ct.ts(21))]


def test_new_rows_after_copied_rows_that_no_compaction_follows_stay_in_the_running_turn(tmp_path):
    """Session 2d924742: Claude Desktop rewrites the conversation with no compaction after it, then a
    notification wakes the running turn. The rows from the notification on are that turn's work, so u2
    builds as it does in the same file without the copy."""
    reply = ct.assistant(2, "a-0", "msg_0", [ct.text("Both screens launched.")], stop_reason="end_turn")
    first, since = _compact(5, 1), [ct.prompt(11, "u1", "continue"),
                                    ct.assistant(12, "a-1", "msg_1", [ct.text("ok")], stop_reason="end_turn")]

    def session(copy):
        return ct.write(tmp_path / str(copy) / "S1.jsonl", [
            ct.prompt(1, "u0", "start"), reply, *first, *since,
            ct.prompt(20, "u2", "run the pilot"),
            ct.assistant(21, "a-2", "msg_2", [ct.text("Launched.")], stop_reason="end_turn"),
            *([*first[:2], reply, *first[2:], *since] if copy else []),
            {**ct.prompt(30, "n1", "<task-notification>pilot finished</task-notification>"),
             "promptSource": "system", "origin": {"kind": "task-notification"}},
            ct.assistant(31, "a-2b", "msg_2b", [ct.text("The pilot finished.")], stop_reason="end_turn"),
            ct.prompt(40, "u3", "next"),
            ct.assistant(41, "a-3", "msg_3", [ct.text("done")], stop_reason="end_turn"),
        ])

    copied, plain = session(True), session(False)
    u2 = _records(copied, "u2")["cases"]
    # Before, the notification and the reply to it went in no turn, and u2 was [prompt 20, reply 21].
    assert [(c["input"]["kind"], c["input"]["started_at"]) for c in u2] == [
        ("user_message", ct.ts(20)), ("assistant_message", ct.ts(21)), ("assistant_message", ct.ts(31))]
    assert u2[0]["output"]["turn_duration_ms"] == 11
    for turn_id in ("u0", "k1", "u1", "u2", "u3"):
        expected = _records(plain, turn_id)
        if turn_id == "u3":                 # the plain file's turn 5; the copied prompts add 2
            expected = {**expected, "cases": [{**c, "input": {**c["input"], "turn_index": 7}} for c in expected["cases"]]}
        assert _records(copied, turn_id) == expected, turn_id


def test_a_turn_ends_at_its_last_message_tool_result_interruption_or_compaction(tmp_path):
    """A turn rebuilt from a finished transcript is followed by rows that aren't its work: the stop hook
    summary, the queue rows of the next prompt, and the summary and caveat of the /compact after it."""
    def queued(ms):
        return {"type": "queue-operation", "operation": "enqueue", "timestamp": ct.ts(ms), "sessionId": ct.SESSION}

    path = ct.write(tmp_path / "S1.jsonl", [
        ct.prompt(1, "u1", "build it"),
        ct.assistant(2, "a-1", "msg_1", [ct.tool_use("toolu_1", "Bash", {"command": "make"})], stop_reason="tool_use"),
        ct.tool_result(4, "r1", "toolu_1", "built"),
        ct.assistant(6, "a-2", "msg_2", [ct.text("Built.")], stop_reason="end_turn"),
        {"type": "system", "subtype": "stop_hook_summary", "uuid": "h1", "timestamp": ct.ts(7), "sessionId": ct.SESSION},
        queued(40),
        *_compact(50, 1),
        ct.prompt(60, "u2", "now test it"),
        ct.assistant(61, "a-3", "msg_3", [ct.tool_use("toolu_2", "Bash", {"command": "make test"})], stop_reason="tool_use"),
        ct.interrupted(63, "i2"),
        queued(90),
    ])

    durations = {turn_id: _records(path, turn_id)["cases"][0]["output"]["turn_duration_ms"]
                 for turn_id, _ in ct_reader.turn_ends(ct_reader.load(path))}
    # Before, u1 ran to the /compact's caveat (49) and u2 to the next queued prompt (30).
    assert durations == {"u1": 5, "k1": 5, "u2": 3}


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


def _queued(ms):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ct.ts(ms), "sessionId": ct.SESSION}


def test_a_session_started_from_another_leaves_out_the_turns_it_copied(tmp_path):
    """Session d258ec50, started in Claude Desktop from ae268659: its file begins with ae268659's rows
    from the last compaction on, uuids and timestamps included, before its own first prompt is queued.
    Those turns are ae268659's and were written under that session."""
    copied = [ct.compact(5, "c1", trigger="manual"),
              ct.prompt(10, "p1", "fix the bug"),
              ct.assistant(11, "a-1", "msg_1", [ct.text("fixed")], stop_reason="end_turn"),
              ct.prompt(20, "p2", "add a test"),
              ct.assistant(21, "a-2", "msg_2", [ct.text("added")], stop_reason="end_turn")]
    t = ct_reader.load(ct.write(tmp_path / "S2.jsonl", [
        {"type": "custom-title", "customTitle": "Ship it", "sessionId": ct.SESSION},
        _queued(99), *copied,
        ct.prompt(100, "b1", "now ship it"),
        ct.assistant(101, "a-3", "msg_3", [ct.text("shipped")], stop_reason="end_turn"),
    ]))

    assert ct_reader.turn_ends(t) == [("b1", "completed")]
    # The copied prompts still count, as copies within one file do.
    assert ct_reader.select_turn(t, "b1").index == 3
    assert [(c["input"]["kind"], c["input"]["started_at"]) for c in _records(t.path, "b1")["cases"]] == [
        ("user_message", ct.ts(100)), ("assistant_message", ct.ts(101))]


def test_a_pr_link_row_stamped_after_the_first_prompt_does_not_hide_it(tmp_path):
    """Sessions 5b3999bb, a2e6c820 and 8cc3dede: Claude Desktop writes a pr-link row among the header rows
    at the top of the file, stamped just after the session's first prompt."""
    t = ct_reader.load(ct.write(tmp_path / "S1.jsonl", [
        {"type": "pr-link", "prNumber": 1, "timestamp": ct.ts(2), "sessionId": ct.SESSION},
        _queued(0),
        ct.prompt(1, "u1", "open a PR"),
        ct.assistant(3, "a-1", "msg_1", [ct.text("opened")], stop_reason="end_turn"),
    ]))

    assert ct_reader.turn_ends(t) == [("u1", "completed")]
    session = ct_reader.session_case(t, ct_reader.select_turn(t, "u1"), _ctx(), _Plain())
    assert session["input"]["started_at"] == ct.ts(0)


def test_a_session_whose_file_copies_another_sessions_whole_file_leaves_out_those_turns(tmp_path):
    """Session dfcc8bec, started in Claude Desktop from 5b3999bb: its file begins with all of 5b3999bb's
    rows, that session's ID included, then its own rows under its own ID."""
    copied = [_queued(0), ct.prompt(1, "p1", "fix the bug"),
              ct.assistant(2, "a-1", "msg_1", [ct.text("fixed")], stop_reason="end_turn")]
    own = [{"type": "custom-title", "customTitle": "Ship it"}, _queued(99), ct.prompt(100, "b1", "now ship it"),
           ct.assistant(101, "a-2", "msg_2", [ct.text("shipped")], stop_reason="end_turn")]
    session = ct.write(tmp_path / "S2.jsonl", [*copied, *[{**row, "sessionId": "S2"} for row in own]])
    # The earlier session's subagent files are copied too. Their launching turns aren't this session's,
    # so CES couldn't root them.
    ct.write_subagent(session, "x1", "toolu_old", 1, [
        ct.prompt(3, "s1", "count", agent="x1"),
        ct.assistant(4, "b-1", "msg_x1", [ct.text("3")], stop_reason="end_turn", agent="x1")])
    own_agent = ct.write_subagent(session, "y1", "toolu_new", 1, [
        {**row, "sessionId": "S2"} for row in (
            ct.prompt(102, "s2", "count", agent="y1"),
            ct.assistant(103, "b-2", "msg_y1", [ct.text("3")], stop_reason="end_turn", agent="y1"))])
    t = ct_reader.load(session)

    assert ct_reader.turn_ends(t) == [("b1", "completed")]
    assert ct_reader.thread_id(t) == "S2"
    assert ct_reader.subagent_paths(t) == [own_agent]
    session = ct_reader.session_case(t, ct_reader.select_turn(t, "b1"), _ctx(), _Plain())
    assert session["input"]["started_at"] == ct.ts(99)
