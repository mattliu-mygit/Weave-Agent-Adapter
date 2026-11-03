"""Hook events to turn jobs: turn sources, subagent turns, the sweep, and the DLQ."""
from __future__ import annotations

import json
import threading
import time

from weave_agent_adapter.ces import CesError
from weave_agent_adapter.model import WireEvent
from weave_agent_adapter.session_sender import SessionSender
import claude_transcripts as cl
import codex_transcripts as ct


class _Plain:
    def scrub(self, value):
        return value


class _Writer:
    def __init__(self, error=None):
        self.jobs, self.error = [], error

    def write_turn(self, job):
        self.jobs.append(job)
        if self.error:
            raise self.error

    def written_turns(self, harness, session_id):
        return set() if self.error else {turn["turn_id"] for job in self.jobs for turn in job["turns"]}


def _sender(tmp_path, writer, **kwargs):
    return SessionSender(writer, str(tmp_path / "dlq.jsonl"), _Plain(), "1.2.3", **kwargs)


def _event(event, at, **payload):
    return WireEvent("codex", event, at, payload)


def _finish(sender):
    deadline = time.monotonic() + 5
    while sender.has_active_work() and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.close()


def test_a_stop_sends_its_turn_with_the_subagent_turns_it_ran(tmp_path):
    parent = ct.write(tmp_path / "parent.jsonl", ct.parent_with_subagent())
    child = ct.write(tmp_path / "child.jsonl", ct.subagent_fork())
    writer = _Writer()
    sender = _sender(tmp_path, writer)

    sender.handle(_event("SessionStart", 1.0, session_id="T3", transcript_path=parent, cwd="/repo"))
    sender.handle(_event("SubagentStop", 2.0, session_id="T3", agent_id="C1", transcript_path=child,
                         agent_transcript_path=child, turn_id="K1"))
    sender.handle(_event("Stop", 3.0, session_id="T3", transcript_path=parent, turn_id="P1"))
    _finish(sender)

    [job] = writer.jobs
    assert (job["harness"], job["session_id"], job["session_case"]["input"]["kind"]) == ("codex", "T3", "session")
    assert [(t["agent_id"], t["turn_id"]) for t in job["turns"]] == [(None, "P1"), ("C1", "K1")]


def test_a_claude_code_stop_waits_briefly_for_its_turn_to_end(tmp_path):
    rows = [cl.prompt(1000, "u1", "hi"), cl.assistant(2000, "a1", "m1", [cl.text("hello")], stop_reason="end_turn")]
    path = cl.write(tmp_path / "s.jsonl", rows[:1])
    writer = _Writer()
    sender = _sender(tmp_path, writer, harnesses=("claude-code",))
    threading.Timer(0.3, cl.write, (tmp_path / "s.jsonl", rows)).start()

    sender.handle(WireEvent("claude-code", "Stop", 1.0, {"session_id": "S1", "transcript_path": path}))
    _finish(sender)

    [job] = writer.jobs
    assert any(case["output"].get("turn_status") == "completed" for case in job["turns"][0]["cases"])


def test_an_idle_sweep_leaves_a_running_last_turn_for_its_stop(tmp_path):
    path = tmp_path / "s.jsonl"
    rows = [cl.prompt(1000, "u1", "build it"), cl.assistant(2000, "a1", "m1", [cl.text("building")])]
    cl.write(path, rows)
    writer = _Writer()
    sender = _sender(tmp_path, writer, harnesses=("claude-code",))
    sender.handle(WireEvent("claude-code", "UserPromptSubmit", 1.0, {"session_id": "S1", "transcript_path": str(path)}))
    sender.sweep(now=10_000.0, ttl=1.0)
    while sender.has_active_work():
        time.sleep(0.01)
    assert writer.jobs == []

    cl.write(path, [*rows, cl.assistant(3000, "a2", "m2", [cl.text("built")], stop_reason="end_turn")])
    sender.handle(WireEvent("claude-code", "Stop", 10_001.0, {"session_id": "S1", "transcript_path": str(path)}))
    _finish(sender)

    [job] = writer.jobs
    assert any(case["output"].get("turn_status") == "completed" for case in job["turns"][0]["cases"])


def test_a_session_end_sends_an_unended_last_turn(tmp_path):
    path = cl.write(tmp_path / "s.jsonl", [cl.prompt(1000, "u1", "hi"), cl.assistant(2000, "a1", "m1", [cl.text("partial")])])
    writer = _Writer()
    sender = _sender(tmp_path, writer, harnesses=("claude-code",))

    sender.handle(WireEvent("claude-code", "SessionEnd", 1.0, {"session_id": "S1", "transcript_path": path}))
    _finish(sender)

    [job] = writer.jobs
    assert [turn["turn_id"] for turn in job["turns"]] == ["u1"]


class _WrittenWriter(_Writer):
    def written_turns(self, harness, session_id):
        return {"P1", "K1"}


def test_a_subagent_turn_that_ends_after_its_parent_was_sent_goes_on_its_own(tmp_path):
    parent = ct.write(tmp_path / "parent.jsonl", ct.parent_with_subagent())
    child = ct.write(tmp_path / "child.jsonl", ct.subagent_fork())
    writer = _Writer()
    sender = _sender(tmp_path, writer)
    sender.handle(_event("Stop", 1.0, session_id="T3", transcript_path=parent, turn_id="P1"))
    while sender.has_active_work():
        time.sleep(0.01)

    sender.handle(_event("SubagentStop", 2.0, session_id="T3", agent_id="C1", agent_transcript_path=child,
                         turn_id="K1"))
    sender.sweep(now=10_000.0, ttl=1.0)
    _finish(sender)

    assert [[(t["agent_id"], t["turn_id"]) for t in job["turns"]] for job in writer.jobs] == [
        [(None, "P1")], [("C1", "K1")]]


def test_a_restarted_sender_does_not_resend_written_subagent_turns(tmp_path):
    parent = ct.write(tmp_path / "parent.jsonl", ct.parent_with_subagent())
    child = ct.write(tmp_path / "child.jsonl", ct.subagent_fork())
    writer = _WrittenWriter()
    sender = _sender(tmp_path, writer)

    sender.handle(_event("SubagentStop", 1.0, session_id="T3", agent_id="C1", agent_transcript_path=child,
                         turn_id="K1"))
    sender.handle(_event("Stop", 2.0, session_id="T3", transcript_path=parent, turn_id="P1"))
    _finish(sender)

    assert writer.jobs == []


def test_a_later_stop_does_not_resend_a_subagent_turn(tmp_path):
    rows = ct.parent_with_subagent()
    parent = ct.write(tmp_path / "parent.jsonl", rows)
    child = ct.write(tmp_path / "child.jsonl", ct.subagent_fork())
    writer = _Writer()
    sender = _sender(tmp_path, writer)
    sender.handle(_event("SubagentStop", 1.0, session_id="T3", agent_id="C1", agent_transcript_path=child, turn_id="K1"))
    sender.handle(_event("Stop", 2.0, session_id="T3", transcript_path=parent, turn_id="P1"))
    while sender.has_active_work():
        time.sleep(0.01)
    ct.write(tmp_path / "parent.jsonl", [*rows, ct.task_started(30, "P2"), ct.user(31, "thanks"),
                                         ct.assistant(32, "Welcome."), ct.usage(33, "P2", "r3", 5, 5, 105),
                                         ct.task_complete(34, "P2")])

    sender.handle(_event("Stop", 3.0, session_id="T3", transcript_path=parent, turn_id="P2"))
    _finish(sender)

    assert [[t["turn_id"] for t in job["turns"]] for job in writer.jobs] == [["P1", "K1"], ["P2"]]


def test_the_sweep_sends_an_interrupted_last_turn(tmp_path):
    path = ct.write(tmp_path / "t.jsonl", ct.interrupted_then_next()[:6])
    writer = _Writer()
    sender = _sender(tmp_path, writer)

    sender.handle(_event("UserPromptSubmit", 1.0, session_id="T2", transcript_path=path))
    sender.sweep(now=10_000.0, ttl=60.0)
    _finish(sender)

    [job] = writer.jobs
    assert job["turns"][0]["turn_id"] == "A1"
    assert job["turns"][0]["cases"][0]["output"]["turn_status"] == "aborted"


def test_a_failed_write_keeps_the_whole_job_in_one_dead_letter_line(tmp_path):
    path = ct.write(tmp_path / "t.jsonl", ct.exec_turn())
    writer = _Writer(error=CesError(503, "unavailable", "down"))
    sender = _sender(tmp_path, writer)

    sender.handle(_event("Stop", 1.0, session_id="T1", transcript_path=path, turn_id="U1"))
    _finish(sender)

    [line] = (tmp_path / "dlq.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert (entry["v"], entry["key"], entry["status"], entry["job"]) == (2, "turn-v1:codex:T1:U1", 503, writer.jobs[0])


def test_only_harnesses_with_a_transcript_reader_are_handled(tmp_path):
    sender = _sender(tmp_path, _Writer(), harnesses=("claude-code", "codex", "gemini-cli"))
    try:
        assert sender.harnesses == {"claude-code", "codex"}
    finally:
        sender.close()


def test_a_claude_stop_sends_the_last_turn_with_its_subagent_turns(tmp_path):
    import claude_transcripts as cl
    session = cl.session_with_task(tmp_path)
    with open(session) as fh:
        first_turn = fh.readlines()[:7]                # the Stop fires before the second prompt exists
    with open(session, "w") as fh:
        fh.writelines(first_turn)
    writer = _Writer()
    sender = _sender(tmp_path, writer, harnesses=("claude-code",))

    sender.handle(WireEvent("claude-code", "Stop", 1.0, {"session_id": cl.SESSION, "transcript_path": session,
                                                         "background_tasks": ["bg-1"]}))
    sender.handle(WireEvent("claude-code", "Stop", 2.0, {"session_id": cl.SESSION, "transcript_path": session}))
    _finish(sender)

    [job] = writer.jobs
    assert job["harness"] == "claude-code"
    assert [(t["agent_id"], t["turn_id"]) for t in job["turns"]] == [(None, "u1"), ("a1", "s1"), ("a2", "n1")]


def test_a_turn_whose_stop_waited_on_background_tasks_goes_with_the_next_turn(tmp_path):
    import claude_transcripts as cl
    first = [cl.prompt(1, "u1", "start a slow job"),
             cl.assistant(2, "a-1", "msg_1", [cl.text("Started in the background.")], stop_reason="end_turn")]
    path = cl.write(tmp_path / f"{cl.SESSION}.jsonl", first)
    writer = _Writer()
    sender = _sender(tmp_path, writer, harnesses=("claude-code",))
    sender.handle(WireEvent("claude-code", "Stop", 1.0, {"session_id": cl.SESSION, "transcript_path": path,
                                                         "background_tasks": ["bg-1"]}))
    cl.write(tmp_path / f"{cl.SESSION}.jsonl", [
        *first, {**cl.prompt(3, "n1", "<task-notification>done</task-notification>"),
                 "promptSource": "system", "origin": {"kind": "task-notification"}},
        cl.prompt(4, "u2", "say hi"), cl.assistant(5, "a-2", "msg_2", [cl.text("hi")], stop_reason="end_turn")])

    sender.handle(WireEvent("claude-code", "Stop", 2.0, {"session_id": cl.SESSION, "transcript_path": path}))
    _finish(sender)

    assert [[t["turn_id"] for t in job["turns"]] for job in writer.jobs] == [["u1"], ["u2"]]


def test_the_sweep_sends_a_finished_turn_no_stop_sent(tmp_path):
    path = ct.write(tmp_path / "t.jsonl", ct.exec_turn())
    writer = _Writer()
    sender = _sender(tmp_path, writer)

    sender.handle(_event("UserPromptSubmit", 1.0, session_id="T1", transcript_path=path))
    sender.sweep(now=10_000.0, ttl=60.0)
    _finish(sender)

    assert [job["turns"][0]["turn_id"] for job in writer.jobs] == ["U1"]
