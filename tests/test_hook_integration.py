"""Every shipped hook traverses the real CLI and Unix socket."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

from weave_agent_adapter.profile import load_profile
from weave_agent_adapter.sidecar import Sidecar


def _wait_for_socket(path: Path) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(path))
                return
            except OSError:
                pass
        time.sleep(0.01)
    raise AssertionError(f"sidecar socket did not start: {path}")


@pytest.mark.parametrize("harness", ["codex", "claude-code", "gemini-cli"])
def test_every_registered_hook_reaches_sidecar(harness, tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    socket_path = repo_root / f".t-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock"
    sidecar = Sidecar("ent/proj", str(socket_path), idle_s=30.0)
    seen = []
    original_handle = sidecar._handle_line

    def recording_handle(raw):
        seen.append(json.loads(raw)["event"])
        original_handle(raw)

    sidecar._handle_line = recording_handle
    thread = threading.Thread(target=sidecar.serve)
    thread.start()
    try:
        _wait_for_socket(socket_path)
        profile = load_profile(harness)
        events = profile.registration["events"]
        env = os.environ.copy()
        env["WEAVE_AGENT_ADAPTER_SOCKET"] = str(socket_path)
        for event in events:
            payload = {
                "session_id": f"integration-{harness}",
                "prompt": "integration prompt",
                "tool_name": "Bash",
                "tool_use_id": f"tool-{event}",
                "tool_response": {"ok": True},
                "agent_id": "agent-1",
                "agent_type": "reviewer",
                "last_assistant_message": "integration reply",
            }
            started = time.monotonic()
            command = [sys.executable, "-m", "weave_agent_adapter", "hook",
                       "--harness", harness]
            if "--success-json" in profile.registration["command"]:
                command.append("--success-json")
            command.extend(["--event", event])
            result = subprocess.run(
                command,
                input=json.dumps(payload), text=True, cwd=repo_root, env=env,
                capture_output=True, timeout=2.0,
            )
            assert result.returncode == 0
            expected_stdout = "{}\n" if harness == "gemini-cli" else ""
            assert result.stdout == expected_stdout
            assert time.monotonic() - started < 1.0
        deadline = time.monotonic() + 1.0
        while len(seen) < len(events) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen == events
    finally:
        sidecar.stop()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        for path in (socket_path, Path(str(socket_path) + ".lock")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
