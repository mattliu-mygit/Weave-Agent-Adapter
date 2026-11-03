"""Hook CLI latency, input validation, and disable semantics."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from weave_agent_adapter import cli
from weave_agent_adapter.diagnostics import diagnose


def test_read_stdin_partial_writer_returns_at_deadline(monkeypatch):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r")
    monkeypatch.setattr(cli.sys, "stdin", reader)

    def partial_writer():
        os.write(write_fd, b"{")
        time.sleep(0.30)
        os.close(write_fd)

    writer = threading.Thread(target=partial_writer)
    writer.start()
    started = time.monotonic()
    try:
        cli._read_stdin(timeout=0.05)
    finally:
        elapsed = time.monotonic() - started
        writer.join()
        reader.close()
    assert elapsed < 0.20


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("yes", True),
     ("0", False), ("false", False), ("no", False), ("", False)],
)
def test_env_truthy_uses_explicit_boolean_values(monkeypatch, value, expected):
    monkeypatch.setenv("WEAVE_AGENT_ADAPTER_DISABLE", value)
    assert cli._env_truthy("WEAVE_AGENT_ADAPTER_DISABLE") is expected


def test_malformed_payload_is_not_forwarded(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "_read_stdin", lambda **kwargs: "{ broken")
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)
    args = argparse.Namespace(harness="codex", event="SessionStart")
    assert cli.cmd_hook(args) == 0
    assert sent == []


def test_success_json_flag_prints_empty_object_even_for_malformed_input(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_stdin", lambda **kwargs: "{ broken")
    args = argparse.Namespace(
        harness="gemini-cli", event="SessionStart", success_json=True)

    assert cli.cmd_hook(args) == 0
    assert capsys.readouterr().out == "{}\n"


def test_hook_parser_keeps_success_json_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_stdin", lambda **kwargs: '{}')
    monkeypatch.setattr(cli.transport, "send", lambda event: True)

    assert cli.main([
        "hook", "--harness", "gemini-cli", "--success-json",
        "--event", "SessionStart",
    ]) == 0
    assert capsys.readouterr().out == "{}\n"


def test_captured_at_is_hook_entry_time_not_post_read_time(monkeypatch):
    sent = []
    times = iter([10.0, 99.0])
    monkeypatch.setattr(cli.time, "time", lambda: next(times))

    def delayed_read(**kwargs):
        cli.time.time()
        return '{"session_id":"s"}'

    monkeypatch.setattr(cli, "_read_stdin", delayed_read)
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)
    args = argparse.Namespace(harness="codex", event="SessionStart")
    assert cli.cmd_hook(args) == 0
    assert sent[0]["captured_at"] == 10.0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, "agent_session"),
        ("judge_evaluation", "judge_evaluation"),
        ("future_role", "other_system"),
    ],
)
def test_hook_forwards_validated_trace_role(tmp_path, monkeypatch, configured, expected):
    sent = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_read_stdin", lambda **_kwargs: '{"session_id":"s"}')
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)
    if configured is None:
        monkeypatch.delenv("WEAVE_AGENT_TRACE_ROLE", raising=False)
    else:
        monkeypatch.setenv("WEAVE_AGENT_TRACE_ROLE", configured)

    assert cli.cmd_hook(argparse.Namespace(harness="codex", event="SessionStart")) == 0

    assert sent[0]["trace_role"] == expected


def _write_workspace_role(directory, value):
    path = directory / ".weave-agent-adapter" / "trace-role"
    path.parent.mkdir(parents=True)
    path.write_text(value)


def test_hook_uses_profile_cwd_for_workspace_trace_role(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "process"
    workspace.mkdir()
    process_cwd.mkdir()
    _write_workspace_role(workspace, "judge_evaluation")
    _write_workspace_role(process_cwd, "signal_evaluation")
    sent = []
    monkeypatch.chdir(process_cwd)
    monkeypatch.delenv("WEAVE_AGENT_TRACE_ROLE", raising=False)
    monkeypatch.setattr(
        cli,
        "_read_stdin",
        lambda **_kwargs: json.dumps({"session_id": "s", "cwd": str(workspace)}),
    )
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)

    assert cli.cmd_hook(argparse.Namespace(harness="codex", event="SessionStart")) == 0

    assert sent[0]["trace_role"] == "judge_evaluation"


def test_hook_without_payload_cwd_uses_process_workspace(tmp_path, monkeypatch):
    _write_workspace_role(tmp_path, "reflection_evaluation")
    sent = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEAVE_AGENT_TRACE_ROLE", raising=False)
    monkeypatch.setattr(cli, "_read_stdin", lambda **_kwargs: '{"session_id":"s"}')
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)

    assert cli.cmd_hook(argparse.Namespace(harness="codex", event="SessionStart")) == 0

    assert sent[0]["trace_role"] == "reflection_evaluation"


def test_missing_profile_falls_back_to_process_workspace(tmp_path, monkeypatch):
    _write_workspace_role(tmp_path, "signal_evaluation")
    sent = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEAVE_AGENT_TRACE_ROLE", raising=False)
    monkeypatch.setattr(cli, "_read_stdin", lambda **_kwargs: '{"session_id":"s"}')
    monkeypatch.setattr(cli.transport, "send", lambda event: sent.append(event) or True)

    assert cli.cmd_hook(argparse.Namespace(harness="missing", event="SessionStart")) == 0

    assert sent[0]["trace_role"] == "signal_evaluation"


def test_ensure_sidecar_has_one_short_deadline(monkeypatch):
    monkeypatch.setattr(cli, "_sidecar_up", lambda: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: object())
    started = time.monotonic()
    assert cli._ensure_sidecar(deadline=0.05) is False
    assert time.monotonic() - started < 0.20


def test_diagnostic_omits_payload_and_exception_message(tmp_path, monkeypatch):
    path = tmp_path / "adapter.log"
    monkeypatch.setenv("WEAVE_AGENT_ADAPTER_LOG", str(path))
    diagnose("payload_parse", harness="codex", event="SessionStart",
             error=ValueError("secret prompt and API key"))
    content = path.read_text()
    assert "phase=payload_parse" in content
    assert "harness=codex" in content
    assert "event=SessionStart" in content
    assert "error=ValueError" in content
    assert "secret prompt" not in content
    assert "API key" not in content
    assert path.stat().st_mode & 0o777 == 0o600


def test_sidecar_command_serves_then_closes_the_session_sender(monkeypatch):
    import weave_agent_adapter.config as config_module
    import weave_agent_adapter.sidecar as sidecar_module

    config = SimpleNamespace(redact_keys=[], redact_enabled=True, idle_shutdown_s=5.0,
                             session_rate=0.5, session_ttl_s=60.0)
    sender = SimpleNamespace(closed=False)
    sender.close = lambda: setattr(sender, "closed", True)
    built = {}

    class FakeSidecar:
        def __init__(self, socket_path, **kwargs):
            built.update(kwargs)

        def serve(self):
            assert not sender.closed

        def stop(self):
            pass

    monkeypatch.setattr(config_module, "load_config", lambda path: config)
    monkeypatch.setattr(sidecar_module, "Sidecar", FakeSidecar)
    monkeypatch.setattr(cli, "_session_sender", lambda cfg, redactor, profiles_dir=None: sender)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    assert cli.cmd_sidecar(SimpleNamespace(config=None, profiles_dir=None)) == 0

    assert (built["idle_s"], built["session_rate"], built["session_ttl"], built["session_sender"]) == (
        5.0, 0.5, 60.0, sender)
    assert sender.closed


def test_codex_install_prints_profile_declared_trust_step(tmp_path, capsys):
    args = SimpleNamespace(
        harness="codex",
        local=False,
        profiles_dir=None,
        settings_path=str(tmp_path / "hooks.json"),
    )

    assert cli.cmd_install(args) == 0
    output = capsys.readouterr().out
    assert "registered codex hooks" in output
    assert "review and trust them with /hooks" in output
