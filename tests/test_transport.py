"""Hook-to-sidecar transport while the sidecar is busy."""
from __future__ import annotations

import json
import os
import socket
import tempfile

import pytest

from weave_agent_adapter import transport


@pytest.mark.parametrize("size", [4_000, 12_000, 20_000, 100_000])
def test_send_delivers_a_whole_event_to_a_busy_sidecar(size):
    # macOS limits Unix socket paths to about 104 bytes, which pytest's tmp_path can exceed.
    with tempfile.TemporaryDirectory(prefix="wa-") as directory:
        path = os.path.join(directory, "sidecar.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            event = {"event": "PostToolUse", "payload": "x" * size}

            # The sidecar accepts nothing until send returns, as when it's busy with another event.
            assert transport.send(event, socket_path=path)

            conn, _ = server.accept()
            with conn:
                received = b"".join(iter(lambda: conn.recv(65536), b""))
        assert json.loads(received) == event
