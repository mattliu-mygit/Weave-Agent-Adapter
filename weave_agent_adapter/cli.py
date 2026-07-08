"""weave-agent-adapter CLI (spec 08): the hook dispatcher and the sidecar runner.

    weave-agent-adapter hook --harness <name> --event <event>   # per hook event
    weave-agent-adapter sidecar [--project ...] [--debug-file ...]
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
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


def cmd_hook(args) -> int:
    # the command-hook adapter: forward the raw payload, never block or break
    try:
        raw = _read_stdin()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}
        transport.send({
            "v": 1, "harness": args.harness, "event": args.event,
            "captured_at": time.time(), "payload": payload, "pid": os.getpid(),
        })
    except Exception:
        pass
    return 0


def cmd_sidecar(args) -> int:
    from .sidecar import Sidecar

    if args.debug_file:
        from .sink import DebugSink
        sink = DebugSink(args.debug_file)
    else:
        from .weave_sink import WeaveSink
        sink = WeaveSink(args.project)

    sc = Sidecar(sink, args.project, transport.SOCKET_PATH, profiles_dir=args.profiles_dir)
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
    s.set_defaults(fn=cmd_sidecar)

    args = p.parse_args(argv)
    return args.fn(args)
