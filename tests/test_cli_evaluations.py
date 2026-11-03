"""`weave-agent-adapter evaluations setup|replay` against a fake CES and GraphQL."""
from __future__ import annotations

import itertools
import os

import pytest

from weave_agent_adapter import ces_writer, cli, config, diagnostics, dlq, records
from weave_agent_adapter.session_sender import SessionSender
from http_fake import FakeCes

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


def test_replay_requires_setup(env):
    _, cfg, _ = env

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 2


def test_replay_reruns_each_dead_turn_job(env, capsys):
    fake, cfg, _ = env
    config.save_evaluations_state({"project": "wandb/matt-test", "name": "agent-sessions",
                                   "scope_ref": "UHJvamVjdDox", "dataset_id": "ds-1"})
    job = {"harness": "codex", "session_id": "T1", "session_case": {"input": {"kind": "session"}, "output": {}},
           "turns": [{"agent_id": None, "parent_agent_id": None, "turn_id": "U1",
                      "cases": [{"input": {"kind": "user_message", "session_trajectory_id": None}, "output": {}}],
                      "spawns": {}}]}
    dlq.append(dlq.DEFAULT_PATH, [dlq.record(job, "turn-v1:codex:T1:U1", 503, "unavailable", 3)])
    _issue_ids(fake)

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0

    assert [h["Idempotency-Key"] for _, h, _ in fake.requests] == [
        "session-v1:codex:T1", "session-trajectory-v1:codex:T1", "turn-v1:codex:T1:U1"]
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
    _, cfg, _ = env
    config.save_evaluations_state({"scope_ref": "S", "dataset_id": "ds-1"})
    with open(dlq.DEFAULT_PATH, "w") as fh:
        fh.write('{"v": 2, "key"')

    assert cli.main(["evaluations", "replay", "--config", cfg]) == 0
    err = capsys.readouterr().err
    assert "1 unreadable" in err and dlq.DEFAULT_PATH + ".corrupt" in err


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
        assert sender.harnesses == {"codex"}
    finally:
        sender.close()
