"""Codex transcript loading and turn selection."""
from __future__ import annotations

import json

import pytest

from weave_agent_adapter import codex_transcript as transcript
import codex_transcripts as ct


def test_select_turn_bounds_rows_and_collects_operations(tmp_path):
    t = transcript.load(ct.write(tmp_path / "r.jsonl", ct.exec_turn()))
    turn = transcript.select_turn(t, "U1")

    assert turn.index == 1 and turn.end_type == "task_complete"
    assert turn.rows[0]["payload"]["type"] == "task_started"
    assert turn.rows[-1]["payload"]["type"] == "task_complete"
    assert turn.context["model"] == "gpt-6"
    assert [op["item"]["id"] for op in turn.operations] == ["exec-1", "exec-2"]
    assert transcript.select_turn(t, "missing") is None


def test_interrupted_turn_has_aborted_end(tmp_path):
    t = transcript.load(ct.write(tmp_path / "r.jsonl", ct.interrupted_then_next()))
    assert transcript.turn_ends(t) == [("A1", "turn_aborted"), ("B1", "task_complete")]
    assert transcript.select_turn(t, "B1").index == 2


def test_subagent_fork_skips_copied_parent_history(tmp_path):
    t = transcript.load(ct.write(tmp_path / "r.jsonl", ct.subagent_fork()))

    # The parent's copied turn is not one of the subagent's own turns.
    assert transcript.turn_ends(t) == [("K1", "task_complete")]
    turn = transcript.select_turn(t, "K1")
    assert turn.index == 1 and turn.parent_turn_id == "P1"
    assert t.meta["id"] == "C1"


def test_torn_final_line_is_ignored_but_a_bad_middle_line_fails(tmp_path):
    path = tmp_path / "r.jsonl"
    ct.write(path, ct.exec_turn())
    with open(path, "a") as fh:
        fh.write('{"timestamp": "2026')                  # Codex mid-write
    assert transcript.select_turn(transcript.load(str(path)), "U1") is not None

    lines = path.read_text().splitlines()
    lines.insert(2, "{not json")
    path.write_text("\n".join(lines))
    with pytest.raises(json.JSONDecodeError):
        transcript.load(str(path))


def test_torn_tail_inside_a_multibyte_character_is_ignored(tmp_path):
    path = tmp_path / "r.jsonl"
    ct.write(path, ct.exec_turn())
    with open(path, "ab") as fh:
        fh.write('{"timestamp": "日'.encode()[:-1])      # tear after 2 of the 3 bytes of 日
    assert transcript.select_turn(transcript.load(str(path)), "U1") is not None


def test_unended_turn_stops_at_the_next_turn(tmp_path):
    rows = [
        ct.session_meta(0, "T9"),
        ct.task_started(1, "A"),                    # Codex killed: no end record for A
        ct.turn_context(2, "A"),
        ct.user(3, "first"),
        ct.task_started(10, "B"),                   # thread resumed
        ct.turn_context(11, "B"),
        ct.user(12, "second"),
        ct.assistant(13, "ok"),
        ct.usage(14, "B", "resp_b", call=50, turn=50, thread=50),
        ct.task_complete(15, "B"),
    ]
    t = transcript.load(ct.write(tmp_path / "r.jsonl", rows))

    a = transcript.select_turn(t, "A")
    assert a.end is None and a.end_type is None
    assert a.context["turn_id"] == "A"
    assert a.rows == t.rows[1:4]
    assert transcript.select_turn(t, "B").index == 2


def test_operations_after_the_end_record_belong_to_their_own_turn(tmp_path):
    rows = ct.exec_turn() + [
        ct.command(60, 70, "U1", "late-1", ["/bin/zsh", "-lc", "late"]),
        ct.command(61, 71, "OTHER", "other-1", ["/bin/zsh", "-lc", "other"]),
    ]
    t = transcript.load(ct.write(tmp_path / "r.jsonl", rows))
    ids = [op["item"]["id"] for op in transcript.select_turn(t, "U1").operations]
    assert ids == ["exec-1", "exec-2", "late-1"]
