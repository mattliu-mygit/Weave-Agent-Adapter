"""weave-agent-adapter CLI (spec 08): the hook dispatcher and the sidecar runner.

    weave-agent-adapter hook --harness <name> --event <event>   # per hook event
    weave-agent-adapter sidecar [--project ...] [--debug-file ...] [--idle-s ...]

The hook lazily spawns the sidecar the first time the socket is unreachable, so
running is zero-touch: the session-start event brings the sidecar up.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

from . import transport


def _read_stdin(timeout: float = 0.5) -> str:
    try:
        if sys.stdin is None or sys.stdin.closed:
            return ""
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read() if ready else ""
    except Exception:
        return ""


def _sidecar_up() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            s.connect(transport.SOCKET_PATH)
        return True
    except OSError:
        return False


def _ensure_sidecar(project: str) -> None:
    if _sidecar_up():
        return
    # detached; the singleton flock means only one survives if several race
    subprocess.Popen(
        [sys.executable, "-m", "weave_agent_adapter", "sidecar", "--project", project],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(300):                 # wait up to ~3s for it to accept
        if _sidecar_up():
            return
        time.sleep(0.01)


def cmd_hook(args) -> int:
    # the command-hook adapter: forward the raw payload, never block or break
    try:
        raw = _read_stdin()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}
        event = {
            "v": 1, "harness": args.harness, "event": args.event,
            "captured_at": time.time(), "payload": payload, "pid": os.getpid(),
        }
        if not transport.send(event):
            _ensure_sidecar(os.environ.get("WEAVE_PROJECT", "weave-agent-adapter"))
            transport.send(event)
    except Exception:
        pass
    return 0


def cmd_sidecar(args) -> int:
    from .sidecar import Sidecar

    debug_file = args.debug_file or os.environ.get("WEAVE_AGENT_ADAPTER_DEBUG_FILE")
    if debug_file:
        from .sinks.debug import DebugSink
        sink = DebugSink(debug_file)
    else:
        from .sinks.weave import WeaveSink
        sink = WeaveSink(args.project)

    idle_s = float(os.environ.get("WEAVE_AGENT_ADAPTER_IDLE_S", args.idle_s))
    sc = Sidecar(sink, args.project, transport.SOCKET_PATH,
                 profiles_dir=args.profiles_dir, idle_s=idle_s)
    signal.signal(signal.SIGTERM, lambda *_: sc.stop())
    signal.signal(signal.SIGINT, lambda *_: sc.stop())
    try:
        sc.serve()
    finally:
        sink.flush()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weave-agent-adapter")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook")
    h.add_argument("--harness", required=True)
    h.add_argument("--event", required=True)
    h.set_defaults(fn=cmd_hook)

    s = sub.add_parser("sidecar")
    s.add_argument("--project", default="weave-agent-adapter")
    s.add_argument("--debug-file")          # write the tree to a file instead of Weave
    s.add_argument("--profiles-dir")
    s.add_argument("--idle-s", type=float, default=120.0)
    s.set_defaults(fn=cmd_sidecar)

    args = p.parse_args(argv)
    return args.fn(args)
