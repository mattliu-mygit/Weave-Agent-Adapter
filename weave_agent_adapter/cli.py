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


def _ensure_sidecar() -> None:
    if _sidecar_up():
        return
    # detached; the singleton flock means only one survives if several race.
    # No args — the sidecar loads its own config (project, redaction, idle).
    subprocess.Popen(
        [sys.executable, "-m", "weave_agent_adapter", "sidecar"],
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
            _ensure_sidecar()
            transport.send(event)
    except Exception:
        pass
    return 0


def cmd_sidecar(args) -> int:
    from .config import load_config
    from .redact import Redactor
    from .sidecar import Sidecar

    cfg = load_config(args.config)
    project = args.project or cfg.project
    os.environ.setdefault(
        "WEAVE_ENABLE_DISK_FALLBACK", "true" if cfg.enable_disk_fallback else "false"
    )

    debug_file = args.debug_file or os.environ.get("WEAVE_AGENT_ADAPTER_DEBUG_FILE")
    if debug_file:
        from .sinks.debug import DebugSink
        sink = DebugSink(debug_file)
    else:
        from .sinks.weave import WeaveSink
        sink = WeaveSink(project)

    redactor = Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled)
    sc = Sidecar(sink, project, transport.SOCKET_PATH, profiles_dir=args.profiles_dir,
                 idle_s=cfg.idle_shutdown_s, redactor=redactor, session_rate=cfg.session_rate)
    signal.signal(signal.SIGTERM, lambda *_: sc.stop())
    signal.signal(signal.SIGINT, lambda *_: sc.stop())
    try:
        sc.serve()
    finally:
        sink.flush()
    return 0


def cmd_install(args) -> int:
    from .install import install
    p = install(args.harness, user=not args.local, profiles_dir=args.profiles_dir,
                path=args.settings_path)
    print(f"registered {args.harness} hooks in {p}")
    return 0


def cmd_uninstall(args) -> int:
    from .install import uninstall
    p = uninstall(args.harness, user=not args.local, profiles_dir=args.profiles_dir,
                  path=args.settings_path)
    print(f"removed weave-agent-adapter hooks from {p}")
    return 0


def cmd_plugin(args) -> int:
    from .install import write_plugin
    d = write_plugin(args.harness, args.dest, profiles_dir=args.profiles_dir)
    print(f"wrote {args.harness} plugin to {d}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weave-agent-adapter")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook")
    h.add_argument("--harness", required=True)
    h.add_argument("--event", required=True)
    h.set_defaults(fn=cmd_hook)

    s = sub.add_parser("sidecar")
    s.add_argument("--project", default=None)   # falls back to config
    s.add_argument("--config")                  # config.toml path (else default)
    s.add_argument("--debug-file")              # write the tree to a file instead of Weave
    s.add_argument("--profiles-dir")
    s.set_defaults(fn=cmd_sidecar)

    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        sp = sub.add_parser(name)
        sp.add_argument("--harness", default="claude-code")
        sp.add_argument("--local", action="store_true",
                        help="write ./.claude/settings.json instead of ~/.claude")
        sp.add_argument("--profiles-dir")
        sp.add_argument("--settings-path")      # override target (testing)
        sp.set_defaults(fn=fn)

    pl = sub.add_parser("plugin", help="write a Claude Code plugin dir (zero-config install)")
    pl.add_argument("--harness", default="claude-code")
    pl.add_argument("--dest", required=True, help="output plugin directory")
    pl.add_argument("--profiles-dir")
    pl.set_defaults(fn=cmd_plugin)

    args = p.parse_args(argv)
    return args.fn(args)
