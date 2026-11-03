"""Bounded hook-to-sidecar Unix-socket transport.

The hook side: send one wire event over the local Unix socket, fire-and-forget.
Bounded so a missing or busy sidecar can never stall a tool call. An event the
sidecar can't take goes to a bounded spool file that the sidecar reads when it runs.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket

SOCKET_PATH = os.path.expanduser(
    os.environ.get("WEAVE_AGENT_ADAPTER_SOCKET", "~/.weave-agent-adapter/sidecar.sock")
)
CONNECT_TIMEOUT_S = 0.25
# The hook reads at most 1 MiB of payload, so the kernel can hold a whole event while the sidecar is busy.
# macOS defaults to 8 KiB, which cut larger events off when the sidecar hadn't accepted yet.
SEND_BUFFER_BYTES = 2 * 1024 * 1024
# Bounded, so a sidecar that never starts can't fill the disk.
SPOOL_MAX_BYTES = 4 * 1024 * 1024


def send(event: dict, socket_path: str = SOCKET_PATH) -> bool:
    """Write one newline-delimited JSON event to the sidecar. False if it can't."""
    line = (json.dumps(event, default=str) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
            s.settimeout(CONNECT_TIMEOUT_S)
            s.connect(socket_path)
            s.sendall(line)
        return True
    except OSError:
        return False


def spool_path(socket_path: str = SOCKET_PATH) -> str:
    return os.path.join(os.path.dirname(socket_path), "spool.jsonl")


def spool(event: dict, socket_path: str = SOCKET_PATH) -> bool:
    """Keep an event the sidecar couldn't take. False if the spool is full or unwritable."""
    line = (json.dumps(event, default=str) + "\n").encode()
    path = spool_path(socket_path)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            if os.fstat(fh.fileno()).st_size + len(line) > SPOOL_MAX_BYTES:
                return False
            fh.write(line)
        return True
    except OSError:
        return False


def take_spool(socket_path: str = SOCKET_PATH) -> list:
    """The spooled event lines, oldest first, leaving the spool empty."""
    try:
        fd = os.open(spool_path(socket_path), os.O_RDWR)
    except OSError:
        return []
    with os.fdopen(fd, "r+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        fh.seek(0)
        fh.truncate()
    return lines
