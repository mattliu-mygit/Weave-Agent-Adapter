"""Ordered CES writes for one turn job, the session file, and replays."""
from __future__ import annotations

import json

import pytest

from weave_agent_adapter import ces_writer, codex_transcript
from weave_agent_adapter.ces import CesClient, CesError
from weave_agent_adapter.ces_writer import CesWriter
from weave_agent_adapter.records import TurnContext
import codex_transcripts as ct
from http_fake import FakeCes

BASE = "/v1/namespaces/wandb/scopes/S/datasets/D"


class _Plain:
    def scrub(self, value):
        return value


@pytest.fixture
def fake():
    f = FakeCes()
    issued = {}

    def respond(method, path, headers, body):
        # CES answers a repeated key and body with the IDs it issued the first time.
        memo = json.dumps([headers.get("Idempotency-Key"), body], sort_keys=True)
        if memo not in issued:
            n = len(issued) + 1
            issued[memo] = ({"case_ids": [f"case-{n}-{i}" for i in range(len(body["cases"]))]}
                            if path.endswith("/cases") else {"trajectory_ids": [f"traj-{n}"]})
        return 200, {}, issued[memo]
    f.respond = respond
    yield f
    f.close()


def _writer(fake, tmp_path):
    return CesWriter(CesClient(fake.url, "key", attempts=1), "S", "D", state_dir=str(tmp_path / "sessions"))


def _job(tmp_path):
    ctx = TurnContext("codex", "T3", "default", "agent_session", "1.2.3", None)
    parent = codex_transcript.load(ct.write(tmp_path / "parent.jsonl", ct.parent_with_subagent()))
    child = codex_transcript.load(ct.write(tmp_path / "child.jsonl", ct.subagent_fork()))
    first = codex_transcript.select_turn(parent, "P1")
    return {"harness": "codex", "session_id": "T3", "session_case": codex_transcript.session_case(parent, first, ctx),
            "turns": [codex_transcript.turn_records(parent, first, {}, ctx, _Plain()),
                      codex_transcript.turn_records(child, codex_transcript.select_turn(child, "K1"), {}, ctx, _Plain())]}


def _keys(requests):
    return [headers.get("Idempotency-Key") for _, headers, _ in requests]


def _failing_once(fake, key, status, code):
    answer = fake.respond

    def respond(method, path, headers, body):
        if headers.get("Idempotency-Key") == key:
            fake.respond = answer
            return status, {}, {"error": {"code": code, "message": "scripted"}}
        return answer(method, path, headers, body)
    fake.respond = respond


def test_a_turn_with_a_subagent_writes_in_order_with_its_keys(fake, tmp_path):
    job = _job(tmp_path)

    _writer(fake, tmp_path).write_turn(job)

    assert [(m, p.removeprefix(BASE)) for m, (p, _, _) in zip(fake.methods, fake.requests)] == [
        ("PATCH", "/cases"), ("POST", "/trajectories"), ("PATCH", "/cases"), ("POST", "/trajectories"), ("PATCH", "/cases")]
    assert _keys(fake.requests) == ["session-v1:codex:T3", "session-trajectory-v1:codex:T3", "turn-v1:codex:T3:P1",
                                    "subagent-v1:codex:T3:C1", "turn-v1:codex:T3:K1"]
    session, s, turn, subagent, subagent_turn = [body for _, _, body in fake.requests]
    assert "trajectory_id" not in session["cases"][0]
    assert s == {"trajectories": [{"name": "codex session T3", "root_case_id": "case-1-0"}]}
    assert {(c["trajectory_id"], c["input"]["session_trajectory_id"]) for c in turn["cases"]} == {("traj-2", "traj-2")}
    spawn = job["turns"][0]["spawns"]["C1"]
    assert subagent == {"trajectories": [{"name": "codex subagent C1", "root_case_id": f"case-3-{spawn}",
                                          "parent_trajectory_id": "traj-2"}]}
    assert {(c["trajectory_id"], c["input"]["session_trajectory_id"]) for c in subagent_turn["cases"]} == {
        ("traj-4", "traj-2")}


def test_a_replay_finishes_a_failed_turn_with_the_original_ids(fake, tmp_path):
    job = _job(tmp_path)
    _failing_once(fake, "subagent-v1:codex:T3:C1", 503, "unavailable")

    with pytest.raises(CesError) as raised:
        _writer(fake, tmp_path).write_turn(job)
    assert raised.value.status == 503
    first = list(fake.requests)
    _writer(fake, tmp_path).write_turn(job)

    replayed = fake.requests[len(first):]
    assert _keys(replayed) == ["turn-v1:codex:T3:P1", "subagent-v1:codex:T3:C1", "turn-v1:codex:T3:K1"]
    assert replayed[0][2] == first[2][2]
    assert replayed[1][2] == first[3][2]


def test_a_reused_key_on_a_turn_counts_as_already_stored(fake, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ces_writer, "diagnose", lambda phase, **_: seen.append(phase))
    _failing_once(fake, "turn-v1:codex:T3:K1", 409, "idempotency_key_reuse")

    _writer(fake, tmp_path).write_turn(_job(tmp_path))

    assert seen == ["ces_already_stored"]


def test_a_restarted_writer_finds_the_same_session_trajectory(fake, tmp_path):
    job = _job(tmp_path)
    _writer(fake, tmp_path).write_turn({**job, "turns": job["turns"][:1]})
    count = len(fake.requests)

    _writer(fake, tmp_path).write_turn({**job, "turns": [{**job["turns"][0], "turn_id": "P2"}]})

    later = fake.requests[count:]
    assert _keys(later) == ["turn-v1:codex:T3:P2"]
    assert {c["trajectory_id"] for c in later[0][2]["cases"]} == {"traj-2"}


def test_a_crash_after_sending_resends_the_stored_session_body(fake, tmp_path):
    stored = {"cases": [{"input": {"kind": "session", "session_id": "T3"}, "output": {}}]}
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "codex-T3.json").write_text(json.dumps({"session_case": {"body": stored}}))

    _writer(fake, tmp_path).write_turn(_job(tmp_path))

    assert (fake.requests[0][1]["Idempotency-Key"], fake.requests[0][2]) == ("session-v1:codex:T3", stored)


def test_a_subagent_whose_launching_turn_was_not_written_waits_for_it(fake, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ces_writer, "diagnose", lambda phase, **_: seen.append(phase))
    job = _job(tmp_path)
    job["turns"][0]["spawns"] = {}
    writer = _writer(fake, tmp_path)

    writer.write_turn(job)

    assert "subagent-v1:codex:T3:C1" not in _keys(fake.requests)
    assert seen == ["ces_subagent_deferred"]
    assert writer.written_turns("codex", "T3") == {"P1"}


def test_written_turns_survive_a_restart(fake, tmp_path):
    _writer(fake, tmp_path).write_turn(_job(tmp_path))

    assert _writer(fake, tmp_path).written_turns("codex", "T3") == {"P1", "K1"}


def test_rollout_context_rides_on_new_trajectories_when_turned_on(fake, tmp_path):
    writer = CesWriter(CesClient(fake.url, "key", attempts=1), "S", "D", state_dir=str(tmp_path / "sessions"),
                       rollout_context=True)

    writer.write_turn(_job(tmp_path))

    bodies = {headers["Idempotency-Key"]: body for _, headers, body in fake.requests}
    session = bodies["session-trajectory-v1:codex:T3"]["trajectories"][0]["rollout_context"]
    subagent = bodies["subagent-v1:codex:T3:C1"]["trajectories"][0]["rollout_context"]
    assert session == {"protocol_version": 1, "role": "evaluated_rollout", "experiment_dataset_id": "D",
                       "client_record_key": "session-trajectory-v1:codex:T3", "evaluated_trajectory_id": None,
                       "models_run_ref": None, "trace_ref": None}
    assert (subagent["role"], subagent["client_record_key"]) == ("child_agent", "subagent-v1:codex:T3:C1")


def test_rollout_context_stays_off_by_default(fake, tmp_path):
    _writer(fake, tmp_path).write_turn(_job(tmp_path))

    assert all("rollout_context" not in t for _, _, body in fake.requests for t in body.get("trajectories", []))


def test_a_reused_key_on_a_spawning_turn_reads_back_its_case_ids(fake, tmp_path):
    job = _job(tmp_path)
    parent = job["turns"][0]
    answer = fake.respond

    def respond(method, path, headers, body):
        if headers.get("Idempotency-Key") == "turn-v1:codex:T3:P1":
            return 409, {}, {"error": {"code": "idempotency_key_reuse", "message": "scripted"}}
        if method == "GET" and "/trajectories/" in path and path.split("?")[0].endswith("/cases"):
            stored = [{"case_id": f"stored-{c['input']['step_index']}",
                       "input": {"turn_id": "P1", "step_index": c["input"]["step_index"]}} for c in parent["cases"]]
            return 200, {}, {"cases": stored, "next_cursor": None}
        return answer(method, path, headers, body)
    fake.respond = respond

    _writer(fake, tmp_path).write_turn(job)

    subagent = next(body for _, h, body in fake.requests if h.get("Idempotency-Key") == "subagent-v1:codex:T3:C1")
    launch = parent["cases"][parent["spawns"]["C1"]]["input"]["step_index"]
    assert subagent["trajectories"][0]["root_case_id"] == f"stored-{launch}"


def test_a_spawning_turn_whose_case_ids_could_not_be_read_back_roots_its_subagent_later(fake, tmp_path):
    job = _job(tmp_path)
    parent, child = job["turns"]
    answer = fake.respond
    reads = []

    def respond(method, path, headers, body):
        if headers.get("Idempotency-Key") == "turn-v1:codex:T3:P1":
            return 409, {}, {"error": {"code": "idempotency_key_reuse", "message": "scripted"}}
        if method == "GET" and "/trajectories/" in path:
            reads.append(path)
            if len(reads) <= 2:  # the read-back after the 409, and this job's retry
                return 503, {}, {"error": {"code": "unavailable", "message": "scripted"}}
            stored = [{"case_id": f"stored-{c['input']['step_index']}",
                       "input": {"turn_id": "P1", "step_index": c["input"]["step_index"]}} for c in parent["cases"]]
            return 200, {}, {"cases": stored, "next_cursor": None}
        return answer(method, path, headers, body)
    fake.respond = respond
    writer = _writer(fake, tmp_path)

    writer.write_turn({**job, "turns": [parent, child]})
    assert "subagent-v1:codex:T3:C1" not in _keys(fake.requests)

    writer.write_turn({**job, "turns": [child]})

    subagent = next(body for _, h, body in fake.requests if h.get("Idempotency-Key") == "subagent-v1:codex:T3:C1")
    launch = parent["cases"][parent["spawns"]["C1"]]["input"]["step_index"]
    assert subagent["trajectories"][0]["root_case_id"] == f"stored-{launch}"


def test_a_final_job_raises_for_a_subagent_that_still_waits(fake, tmp_path):
    job = _job(tmp_path)
    job["turns"][0]["spawns"] = {}
    writer = _writer(fake, tmp_path)

    with pytest.raises(ces_writer.SubagentDeferred) as raised:
        writer.write_turn(job, final=True)

    assert raised.value.code == "subagent_deferred"
    assert writer.written_turns("codex", "T3") == {"P1"}


def test_a_subagent_is_rooted_at_its_launch_site_once_its_launching_turn_is_written(fake, tmp_path):
    job = _job(tmp_path)
    parent, child = job["turns"]
    parent["spawns"] = {}       # the launching turn was written before the subagent's files existed
    writer = _writer(fake, tmp_path)
    writer.write_turn({**job, "turns": [parent]})
    call = next(c["input"] for c in parent["cases"] if c["input"].get("call_id"))
    answer = fake.respond

    def respond(method, path, headers, body):
        if method == "GET" and "/trajectories/" in path:
            stored = [{"case_id": f"stored-{c['input']['step_index']}",
                       "input": {"turn_id": "P1", "step_index": c["input"]["step_index"],
                                 "call_id": c["input"].get("call_id")}} for c in parent["cases"]]
            return 200, {}, {"cases": stored, "next_cursor": None}
        return answer(method, path, headers, body)
    fake.respond = respond

    writer.write_turn({**job, "turns": [child], "launch_sites": {"C1": ["P1", call["call_id"]]}})

    subagent = next(body for _, h, body in fake.requests if h.get("Idempotency-Key") == "subagent-v1:codex:T3:C1")
    assert subagent["trajectories"][0]["root_case_id"] == f"stored-{call['step_index']}"


def test_named_subagent_transcripts_survive_a_restarted_writer(fake, tmp_path):
    _writer(fake, tmp_path).remember_agent_path("codex", "T3", "/a.jsonl")
    _writer(fake, tmp_path).remember_agent_path("codex", "T3", "/a.jsonl")

    assert _writer(fake, tmp_path).agent_paths("codex", "T3") == ["/a.jsonl"]
