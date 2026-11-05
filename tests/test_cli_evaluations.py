"""`weave-agent-adapter evaluations setup|replay|log-transcript` against a fake CES and GraphQL."""
from __future__ import annotations

import dataclasses
import itertools
import json
import os

import pytest

from weave_agent_adapter import ces_writer, cli, config, diagnostics, dlq, records
from weave_agent_adapter.records import TurnContext
from weave_agent_adapter.session_sender import SessionSender
from http_fake import FakeCes
import codex_transcripts as ct

SCOPE = "/v1/namespaces/wandb/scopes/UHJvamVjdDox"


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeCes()
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".netrc").write_text("machine 127.0.0.1 login user password qa-key\n")
    (tmp_path / ".netrc").chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[evaluations]\nenabled = true\nbase_url = "{fake.url}"\n'
                   f'wandb_base_url = "{fake.url}"\nproject = "wandb/matt-test"\n')
    monkeypatch.setattr(config, "EVALUATIONS_STATE_PATH", str(tmp_path / "evaluations.json"))
    monkeypatch.setattr(dlq, "DEFAULT_PATH", str(tmp_path / "dlq.jsonl"))
    monkeypatch.setattr(ces_writer, "DEFAULT_STATE_DIR", str(tmp_path / "sessions"))
    yield fake, str(cfg), tmp_path
    fake.close()


def _issue_ids(fake):
    counter = itertools.count(1)

    def respond(method, path, headers, body):
        if path.endswith("/fields"):
            return 200, {}, {"fields": []}
        if path.endswith("/cases"):
            return 200, {}, {"case_ids": [f"case-{next(counter)}" for _ in body["cases"]]}
        return 200, {}, {"trajectory_ids": [f"traj-{next(counter)}" for _ in body["trajectories"]]}
    fake.respond = respond


def test_setup_creates_the_dataset_declares_fields_and_saves_ids(env, capsys):
    fake, cfg, _ = env
    fake.script = [
        (200, {}, {"data": {"project": {"internalId": "UHJvamVjdDox"}}}),
        (200, {}, {"dataset_id": "ds-1"}),
        (200, {}, {"fields": []}),
    ]

    assert cli.main(["evaluations", "setup", "--config", cfg]) == 0

    graphql, dataset, fields = fake.requests
    assert graphql[0] == "/graphql"
    assert dataset[0] == f"{SCOPE}/datasets"
    assert dataset[1]["Idempotency-Key"] == "dataset-v1:wandb/matt-test:agent-sessions"
    assert dataset[2]["name"] == "agent-sessions"
    assert fields[0] == f"{SCOPE}/datasets/ds-1/fields"
    assert "Idempotency-Key" not in fields[1]
    assert fields[2]["fields"] == records.fields()
    assert config.load_evaluations_state() == {
        "project": "wandb/matt-test", "name": "agent-sessions", "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"}


def test_setup_needs_an_entity_and_project(env, capsys):
    fake, _, tmp_path = env
    cfg = tmp_path / "bare.toml"
    cfg.write_text(f'[evaluations]\nbase_url = "{fake.url}"\nwandb_base_url = "{fake.url}"\nproject = "bare"\n')

    assert cli.main(["evaluations", "setup", "--config", str(cfg)]) == 2
    assert fake.requests == []


def _arms_responder(fake):
    _issue_ids(fake)
    issue = fake.respond

    def respond(method, path, headers, body):
        if path.endswith("/datasets"):
            return 200, {}, {"dataset_id": "ds-arms"}
        return issue(method, path, headers, body)
    fake.respond = respond


def test_log_transcript_logs_every_turn_to_the_named_dataset(env, capsys, tmp_path):
    fake, cfg, home = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    transcript = ct.write(tmp_path / "arm.jsonl", ct.interrupted_then_next(thread="ARM"))
    _arms_responder(fake)

    assert cli.main(["evaluations", "log-transcript", "--harness", "codex", "--file", transcript,
                     "--config", cfg]) == 0

    dataset, fields, *writes = fake.requests
    assert dataset[0] == f"{SCOPE}/datasets"
    assert dataset[1]["Idempotency-Key"] == "dataset-v1:wandb/matt-test:ab-arms"
    assert dataset[2]["name"] == "ab-arms"
    assert fields[0] == f"{SCOPE}/datasets/ds-arms/fields"
    assert {path for path, _, _ in writes} == {
        f"{SCOPE}/datasets/ds-arms/cases", f"{SCOPE}/datasets/ds-arms/trajectories"}
    assert [h["Idempotency-Key"] for _, h, _ in writes][:2] == [
        "session-v1:codex:ARM", "session-trajectory-v1:codex:ARM"]
    assert len([h for _, h, _ in writes if h["Idempotency-Key"].startswith("turn-v1:codex:ARM:")]) == 2
    assert json.loads(capsys.readouterr().out) == {"session_case_id": "case-1", "session_trajectory_id": "traj-2"}
    files = os.listdir(home / "sessions" / "ds-arms")
    assert [name for name in files if not name.endswith(".lock")] == ["codex-ARM.json"]


def test_log_transcript_needs_a_harness_and_a_file(env):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})

    assert cli.main(["evaluations", "log-transcript", "--config", cfg]) == 2
    assert fake.requests == []


def test_replay_requires_setup(env):
    _, cfg, _ = env

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 2


def _dead_turn(turn_id):
    job = {"harness": "codex", "session_id": "T1", "session_case": {"input": {"kind": "session"}, "output": {}},
           "turns": [{"agent_id": None, "parent_agent_id": None, "turn_id": turn_id,
                      "cases": [{"input": {"kind": "user_message", "session_trajectory_id": None}, "output": {}}],
                      "spawns": {}}]}
    return dlq.record(job, f"turn-v1:codex:T1:{turn_id}", 503, "unavailable", 3)


def test_replay_reruns_each_dead_turn_job(env, capsys):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    dlq.append(dlq.DEFAULT_PATH, [_dead_turn("U1")])
    _issue_ids(fake)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0

    _, *writes = fake.requests
    assert [h["Idempotency-Key"] for _, h, _ in writes] == [
        "session-v1:codex:T1", "session-trajectory-v1:codex:T1", "turn-v1:codex:T1:U1"]
    assert not os.path.exists(dlq.DEFAULT_PATH)


def test_replay_declares_the_fields_once_before_its_first_resend(env, capsys):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    dlq.append(dlq.DEFAULT_PATH, [_dead_turn("U1"), _dead_turn("U2")])
    _issue_ids(fake)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0

    fields, *writes = fake.requests
    assert fields[0] == f"{SCOPE}/datasets/ds-1/fields"
    assert fields[2]["fields"] == records.fields()
    assert [path for path, _, _ in writes if path.endswith("/fields")] == []
    assert [h["Idempotency-Key"] for _, h, _ in writes][-2:] == ["turn-v1:codex:T1:U1", "turn-v1:codex:T1:U2"]


def test_replay_resends_the_turns_when_the_field_declaration_fails(env, capsys):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    dlq.append(dlq.DEFAULT_PATH, [_dead_turn("U1")])
    fake.script = [(400, {}, {"error": {"code": "invalid_field", "message": "bad field"}})]
    _issue_ids(fake)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0

    assert fake.requests[-1][1]["Idempotency-Key"] == "turn-v1:codex:T1:U1"
    assert "declaring the Dataset's fields failed" in capsys.readouterr().err
    assert not os.path.exists(dlq.DEFAULT_PATH)


def test_replay_reports_a_replay_already_running(env, monkeypatch, capsys):
    _, cfg, _ = env
    config.save_evaluations_state({"scope_ref": "S", "dataset_id": "ds-1"})

    def busy(path, send):
        raise dlq.ReplayInProgress("another replay is already running")
    monkeypatch.setattr(dlq, "replay", busy)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 1
    assert "already running" in capsys.readouterr().err


def test_replay_reports_lines_set_aside_as_corrupt(env, capsys):
    fake, cfg, _ = env
    config.save_evaluations_state({"scope_ref": "S", "dataset_id": "ds-1"})
    with open(dlq.DEFAULT_PATH, "w") as fh:
        fh.write('{"v": 2, "key"')

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0
    err = capsys.readouterr().err
    assert "1 unreadable" in err and dlq.DEFAULT_PATH + ".corrupt" in err
    assert fake.requests == []          # nothing to resend, so no field declaration either


# ---- `_session_sender`: optional, so it must never raise ----

SAVED = {"project": "wandb/matt-test", "name": "agent-sessions", "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"}


def _sender_cfg(fake, enabled=True):
    return config.Config(evaluations=config.EvaluationsConfig(
        enabled=enabled, base_url=fake.url, wandb_base_url=fake.url, project="wandb/matt-test"))


def _diagnostics_log():
    with open(diagnostics.diagnostic_path()) as fh:
        return fh.read()


def test_session_sender_is_none_when_evaluations_are_disabled(env):
    fake, _, _ = env
    config.save_evaluations_state(SAVED)

    assert cli._session_sender(_sender_cfg(fake, enabled=False), None) is None


def test_session_sender_is_none_before_setup(env):
    fake, _, _ = env

    assert cli._session_sender(_sender_cfg(fake), None) is None


def test_session_sender_is_none_when_state_lacks_the_dataset_id(env):
    fake, _, _ = env
    config.save_evaluations_state({k: v for k, v in SAVED.items() if k != "dataset_id"})

    assert cli._session_sender(_sender_cfg(fake), None) is None
    assert "phase=ces_config " in _diagnostics_log()


@pytest.mark.parametrize("stale", [{"project": "other/project"}, {"name": "renamed-dataset"}])
def test_session_sender_is_none_when_state_is_for_another_project_or_dataset(env, stale):
    fake, _, _ = env
    config.save_evaluations_state({**SAVED, **stale})

    assert cli._session_sender(_sender_cfg(fake), None) is None
    assert "phase=ces_config_mismatch" in _diagnostics_log()


@pytest.mark.parametrize("content", ["[]", '"x"', "[1]"])
def test_session_sender_is_none_when_the_state_file_is_not_an_object(env, content):
    fake, _, tmp_path = env
    (tmp_path / "evaluations.json").write_text(content)

    assert cli._session_sender(_sender_cfg(fake), None) is None


def test_session_sender_is_none_without_a_netrc_entry(env):
    fake, _, tmp_path = env
    config.save_evaluations_state(SAVED)
    (tmp_path / ".netrc").write_text("machine other.example login user password k\n")

    assert cli._session_sender(_sender_cfg(fake), None) is None
    assert "phase=ces_config " in _diagnostics_log()


def test_session_sender_is_built_when_state_and_key_match(env):
    fake, _, _ = env
    config.save_evaluations_state(SAVED)

    sender = cli._session_sender(_sender_cfg(fake), None)
    try:
        assert isinstance(sender, SessionSender)
        assert sender.harnesses == {"claude-code", "codex"}
    finally:
        sender.close()


def test_status_reports_the_turns_waiting_in_the_dead_letter_file(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from weave_agent_adapter import cli, dlq

    path = tmp_path / "dlq.jsonl"
    path.write_text(
        json.dumps({"failed_at": 1_800_000_000.0, "error_code": "unavailable"}) + "\n"
        + json.dumps({"failed_at": 1_800_000_100.0, "error_code": "unavailable"}) + "\n"
        + '{"partial'
    )
    monkeypatch.setattr(dlq, "DEFAULT_PATH", str(path))

    assert cli.cmd_evaluations(SimpleNamespace(action="status", config=None)) == 0

    status = json.loads(capsys.readouterr().out)
    assert (status["turns_waiting"], status["error_codes"]) == (2, {"unavailable": 2})
    assert status["oldest_failure"].startswith("2027-01-15T08:00:00")


def test_replay_rebuilds_a_session_whose_turns_failed_to_build(env, tmp_path):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    transcript = ct.write(tmp_path / "t.jsonl", ct.exec_turn())
    ctx = dataclasses.asdict(TurnContext("codex", "T1", "default", "agent_session", "1.2.3", None))
    dlq.append(dlq.DEFAULT_PATH, [dlq.rebuild_record(
        {"transcript": transcript, "agents": [], "ctx": ctx, "final": True}, "ValueError")])
    _issue_ids(fake)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0

    _, *writes = fake.requests
    assert [h["Idempotency-Key"] for _, h, _ in writes] == [
        "session-v1:codex:T1", "session-trajectory-v1:codex:T1", "turn-v1:codex:T1:U1"]
    assert not os.path.exists(dlq.DEFAULT_PATH)
