"""The Case field contract and the Codex turn records."""
from __future__ import annotations

from weave_agent_adapter import codex_transcript, records
from weave_agent_adapter.records import ToolFacts, TurnContext
import codex_transcripts as ct

TYPES = {"string": str, "integer": int, "boolean": bool, "json": (dict, list)}


class _Plain:
    def scrub(self, value):
        return value


def _ctx(session="T1"):
    return TurnContext("codex", session, "default", "agent_session", "1.2.3", "cfg123")


def _turn(tmp_path, rows, turn_id, facts=None, session="T1", redactor=None):
    t = codex_transcript.load(ct.write(tmp_path / "t.jsonl", rows))
    return codex_transcript.turn_records(t, codex_transcript.select_turn(t, turn_id), facts or {},
                                         _ctx(session), redactor or _Plain())


def _assert_typed(values, declared):
    for name, value in values.items():
        if value is not None:
            expected = TYPES[declared[name]]
            assert isinstance(value, expected) and not (expected is int and isinstance(value, bool)), (name, value)


def test_a_name_declared_in_several_groups_has_one_type():
    for group in (records.COMMON_INPUT, records.SESSION_INPUT, records.MEMBER_INPUT, *records.KIND_INPUT.values()):
        for name, value_type in group.items():
            assert records.INPUT_FIELDS[name] == value_type, name
    for group in (*records.KIND_OUTPUT.values(), records.TURN_TOTALS):
        for name, value_type in group.items():
            assert records.OUTPUT_FIELDS[name] == value_type, name


def test_every_kind_emits_exactly_its_declared_keys_and_types(tmp_path):
    rows = ct.exec_turn()
    turns = [_turn(tmp_path, rows, "U1"),
             _turn(tmp_path, ct.interrupted_then_next(), "A1", session="T2"),
             _turn(tmp_path, ct.subagent_fork(), "K1", session="T3"),
             _turn(tmp_path, ct.parent_with_subagent(), "P1", session="T3"),
             _turn(tmp_path, [*rows[:7], ct.compaction(8), *rows[7:]], "U1")]
    cases = [case for turn in turns for case in turn["cases"]]

    assert {c["input"]["kind"] for c in cases} == set(records.KINDS) - {"session"}
    for case in cases:
        kind = case["input"]["kind"]
        assert set(case["input"]) == {*records.COMMON_INPUT, *records.MEMBER_INPUT, *records.KIND_INPUT[kind]}
        totals = records.TURN_TOTALS if case["input"]["step_index"] == 0 else {}
        assert set(case["output"]) == {*records.KIND_OUTPUT[kind], *totals}
        _assert_typed(case["input"], records.INPUT_FIELDS)
        _assert_typed(case["output"], records.OUTPUT_FIELDS)
    t = codex_transcript.load(ct.write(tmp_path / "s.jsonl", rows))
    session = codex_transcript.session_case(t, codex_transcript.select_turn(t, "U1"), _ctx())
    assert set(session["input"]) == {*records.COMMON_INPUT, *records.SESSION_INPUT}
    _assert_typed(session["input"], records.INPUT_FIELDS)
    assert session["output"] == {}


def test_repo_git_branch_and_day_are_declared_strings_on_every_root_case():
    for name in ("repo", "git_branch", "day"):
        assert name in records.SESSION_INPUT and name in records.MEMBER_INPUT
        assert {"source": "input", "name": name, "value_type": "string"} in records.fields()


def test_day_is_the_utc_date_the_session_started():
    def session_day(timestamp):
        return records.session_case(_ctx(), {}, records.iso(records.ms(timestamp)))["input"]["day"]

    assert session_day("2026-10-06T23:59:59.999Z") == "2026-10-06"
    assert session_day("2026-10-06T20:30:00-04:00") == "2026-10-07"
    assert session_day(None) is None


def test_the_session_case_and_a_subagents_root_case_carry_repo_branch_and_day(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_transcript, "repo_identity", {"/repo": "o/r"}.get)
    t = codex_transcript.load(ct.write(tmp_path / "s.jsonl", ct.parent_with_subagent()))
    session = codex_transcript.session_case(t, codex_transcript.select_turn(t, "P1"), _ctx("T3"))
    turn = _turn(tmp_path, ct.parent_with_subagent(), "P1", session="T3")
    root = turn["cases"][turn["spawns"]["C1"]]["input"]

    expected = {"repo": "o/r", "git_branch": "main", "day": "2026-10-06"}
    assert {k: session["input"][k] for k in expected} == expected
    assert {k: root[k] for k in expected} == expected


def test_an_exec_turn_becomes_a_prompt_two_answers_and_a_tool_call(tmp_path):
    turn = _turn(tmp_path, ct.exec_turn(), "U1")
    cases = turn["cases"]

    assert [c["input"]["kind"] for c in cases] == ["user_message", "assistant_message", "tool_call", "assistant_message"]
    assert [c["input"]["step_index"] for c in cases] == [0, 1, 2, 3]
    user, first, tool, last = cases
    assert (user["input"]["text"], user["input"]["steering"], user["input"]["started_at"]) == ("list files", False, ct.ts(6))
    assert (user["input"]["model"], user["input"]["effort"], user["input"]["git_branch"], user["input"]["config_version"]) == (
        "gpt-6", "high", "main", "cfg123")
    assert (first["output"]["text"], first["output"]["reasoning"], first["output"]["finish_reason"]) == (
        "Listing.", "Need to list.", "tool_calls")
    assert (first["output"]["input_tokens"], first["output"]["output_tokens"]) == (90, 10)
    assert tool["input"]["call_id"] == "call_A"
    assert tool["input"]["arguments"] == {"value": "tools.exec('ls'); tools.exec('pwd')"}
    assert {k: tool["output"][k] for k in ("status", "output", "exit_code", "error", "duration_ms")} == {
        "status": "completed", "output": "a.txt", "exit_code": 1, "error": "no", "duration_ms": 29}
    assert last["output"]["finish_reason"] == "stop"
    assert {k: user["output"][k] for k in ("turn_status", "turn_duration_ms", "turn_input_tokens",
                                           "turn_tool_error_count", "turn_steering_count")} == {
        "turn_status": "completed", "turn_duration_ms": 1000, "turn_input_tokens": 210,
        "turn_tool_error_count": 1, "turn_steering_count": 0}
    assert (turn["agent_id"], turn["spawns"]) == (None, {})


def test_a_message_typed_mid_turn_is_steering(tmp_path):
    rows = ct.exec_turn()
    turn = _turn(tmp_path, [*rows[:-1], ct.user(51, "also show hidden files"), rows[-1]], "U1")

    users = [c for c in turn["cases"] if c["input"]["kind"] == "user_message"]
    assert [u["input"]["steering"] for u in users] == [False, True]
    assert turn["cases"][0]["output"]["turn_steering_count"] == 1


def test_a_permission_prompt_on_an_operation_folds_into_its_tool_call(tmp_path):
    turn = _turn(tmp_path, ct.exec_turn(), "U1", facts={"exec-2": ToolFacts("Bash", permission_requested=True)})

    tool = next(c for c in turn["cases"] if c["input"]["kind"] == "tool_call")
    assert tool["output"]["permission_decision"] == "not_approved"
    assert turn["cases"][0]["output"]["turn_denial_count"] == 1


def test_an_interrupted_turn_reads_aborted(tmp_path):
    turn = _turn(tmp_path, ct.interrupted_then_next(), "A1", session="T2")

    assert [c["input"]["kind"] for c in turn["cases"]] == ["user_message"]
    assert turn["cases"][0]["output"]["turn_status"] == "aborted"


def test_a_subagent_turn_starts_with_the_message_its_parent_sent(tmp_path):
    turn = _turn(tmp_path, ct.subagent_fork(), "K1", session="T3")

    assert (turn["agent_id"], turn["parent_agent_id"]) == ("C1", None)
    first = turn["cases"][0]["input"]
    assert (first["kind"], first["text"], first["steering"], first["agent_id"]) == ("user_message", "count files", False, "C1")


def test_a_spawn_call_names_the_subagent_it_started(tmp_path):
    turn = _turn(tmp_path, ct.parent_with_subagent(), "P1", session="T3")

    assert turn["cases"][turn["spawns"]["C1"]]["input"]["call_id"] == "call_S"


def test_values_are_scrubbed_and_names_are_not(tmp_path):
    class Upper:
        def scrub(self, value):
            return value.upper() if isinstance(value, str) else value

    turn = _turn(tmp_path, ct.exec_turn(), "U1", redactor=Upper())

    assert turn["cases"][0]["input"]["text"] == "LIST FILES"
    assert "turn_input_tokens" in turn["cases"][0]["output"]
