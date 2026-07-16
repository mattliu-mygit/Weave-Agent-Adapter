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
from .diagnostics import diagnose, open_diagnostic_stream


def _read_stdin(timeout: float = 0.5, max_bytes: int = 1_048_576) -> str:
    """Read stdin without waiting past *timeout* or beyond *max_bytes*."""
    fd = None
    was_blocking = None
    try:
        if sys.stdin is None or sys.stdin.closed:
            return ""
        fd = sys.stdin.fileno()
        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        deadline = time.monotonic() + max(0.0, timeout)
        chunks = bytearray()
        while True:
            try:
                chunk = os.read(fd, min(65_536, max_bytes - len(chunks) + 1))
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    break
                continue
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise ValueError("hook payload exceeds size limit")
        encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
        return bytes(chunks).decode(encoding)
    except (OSError, UnicodeError):
        return ""
    finally:
        if fd is not None and was_blocking is not None:
            try:
                os.set_blocking(fd, was_blocking)
            except OSError:
                pass


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _sidecar_up() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            s.connect(transport.SOCKET_PATH)
        return True
    except OSError:
        return False


def _ensure_sidecar(deadline: float = 0.5) -> bool:
    if _sidecar_up():
        return True
    # detached; the singleton flock means only one survives if several race.
    # No args, the sidecar loads its own config (project, redaction, idle).
    log_stream = None
    try:
        log_stream = open_diagnostic_stream()
        subprocess.Popen(
            [sys.executable, "-m", "weave_agent_adapter", "sidecar"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=log_stream,
        )
    except Exception as exc:
        diagnose("sidecar_spawn", error=exc)
        return False
    finally:
        if log_stream is not None:
            log_stream.close()
    stop_at = time.monotonic() + max(0.0, deadline)
    while time.monotonic() < stop_at:
        if _sidecar_up():
            return True
        time.sleep(min(0.01, max(0.0, stop_at - time.monotonic())))
    return _sidecar_up()


def cmd_hook(args) -> int:
    captured_at = time.time()
    if _env_truthy("WEAVE_AGENT_ADAPTER_DISABLE"):
        return 0
    # the command-hook adapter: forward the raw payload, never block or break
    try:
        raw = _read_stdin()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            diagnose("payload_parse", harness=args.harness, event=args.event, error=exc)
            return 0
        if not isinstance(payload, dict):
            diagnose("payload_type", harness=args.harness, event=args.event)
            return 0
        event = {
            "v": 1, "harness": args.harness, "event": args.event,
            "captured_at": captured_at, "payload": payload, "pid": os.getpid(),
        }
        if not transport.send(event):
            if _ensure_sidecar():
                if not transport.send(event):
                    diagnose("socket_send", harness=args.harness, event=args.event)
            else:
                diagnose("sidecar_unavailable", harness=args.harness, event=args.event)
    except Exception as exc:
        diagnose("hook", harness=args.harness, event=args.event, error=exc)
    return 0


def cmd_sidecar(args) -> int:
    from .config import load_config
    from .emit import GenAITurnEmitter
    from .redact import Redactor
    from .sidecar import Sidecar

    cfg = load_config(args.config)
    project = args.project or cfg.project

    debug_file = args.debug_file or os.environ.get("WEAVE_AGENT_ADAPTER_DEBUG_FILE")
    if debug_file:
        # render the same turn trees, but append them to a local file instead
        def _to_file(node, project_id, _path=debug_file):
            with open(_path, "a") as fh:
                fh.write(json.dumps({"project": project_id, "turn": node}, default=str) + "\n")
        turn_emitters = [GenAITurnEmitter(emit=_to_file)]
    else:
        turn_emitters = [GenAITurnEmitter()]

    redactor = Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled)
    sc = Sidecar(project, transport.SOCKET_PATH, profiles_dir=args.profiles_dir,
                 idle_s=cfg.idle_shutdown_s, redactor=redactor, session_rate=cfg.session_rate,
                 session_ttl=cfg.session_ttl_s, project_per_repo=cfg.project_per_repo,
                 turn_emitters=turn_emitters, turn_linger=cfg.turn_linger_s)
    signal.signal(signal.SIGTERM, lambda *_: sc.stop())
    signal.signal(signal.SIGINT, lambda *_: sc.stop())
    try:
        sc.serve()
    finally:
        sc.flush_emitters()
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
