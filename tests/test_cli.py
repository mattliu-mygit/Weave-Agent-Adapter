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
from weave_agent_adapter.model import Session, Turn


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


def test_debug_sidecar_writes_structured_turn_payload(tmp_path, monkeypatch):
    import weave_agent_adapter.config as config_module
    import weave_agent_adapter.sidecar as sidecar_module

    debug_path = tmp_path / "turns.jsonl"
    config = SimpleNamespace(
        project="ent/proj",
        redact_keys=[],
        redact_enabled=True,
        idle_shutdown_s=120.0,
        session_rate=1.0,
        session_ttl_s=3600.0,
        project_per_repo=False,
    )

    class FakeSidecar:
        def __init__(self, *args, emitter=None, **kwargs):
            self.emitter = emitter

        def serve(self):
            self.emitter.emit_turn(
                Turn(started_at=1.0, ended_at=2.0, input_text="hello"),
                Session(session_id="s1", project="ent/proj", last_activity=2.0),
            )

        def flush_emitter(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(config_module, "load_config", lambda path: config)
    monkeypatch.setattr(sidecar_module, "Sidecar", FakeSidecar)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    args = SimpleNamespace(
        config=None,
        project=None,
        debug_file=str(debug_path),
        profiles_dir=None,
    )
    assert cli.cmd_sidecar(args) == 0

    record = json.loads(debug_path.read_text().splitlines()[0])
    assert record["project"] == "ent/proj"
    assert record["turn"]["conversation_id"] == "s1"
    assert record["turn"]["messages"][0]["role"] == "user"
    assert record["turn"]["messages"][0]["content"] == "hello"
    assert record["turn"]["spans"] == []
    assert debug_path.stat().st_mode & 0o777 == 0o600


def test_debug_sidecar_tightens_existing_file_permissions(tmp_path, monkeypatch):
    path = tmp_path / "turns.jsonl"
    path.write_text("")
    path.chmod(0o644)

    cli._append_private_jsonl(str(path), {"ok": True})

    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"ok": True}


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
