"""Hook → sidecar transport (spec 03).

The hook side: send one wire event over the local Unix socket, fire-and-forget.
Bounded so a missing or busy sidecar can never stall a tool call.
"""
from __future__ import annotations

import json
import os
import socket

SOCKET_PATH = os.path.expanduser(
    os.environ.get("WEAVE_AGENT_ADAPTER_SOCKET", "~/.weave-agent-adapter/sidecar.sock")
)
CONNECT_TIMEOUT_S = 0.25


def send(event: dict, socket_path: str = SOCKET_PATH) -> bool:
    """Write one newline-delimited JSON event to the sidecar. False if it can't."""
    line = (json.dumps(event, default=str) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT_S)
            s.connect(socket_path)
            s.sendall(line)
        return True
    except OSError:
        return False
