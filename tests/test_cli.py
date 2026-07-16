"""Hook CLI latency, input validation, and disable semantics."""
from __future__ import annotations

import argparse
import os
import threading
import time

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
