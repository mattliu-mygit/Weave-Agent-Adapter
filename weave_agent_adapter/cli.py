"""Hook dispatcher, sidecar runner, and profile-driven installer CLI.

    weave-agent-adapter hook --harness <name> --event <event>   # per hook event
    weave-agent-adapter sidecar [--project ...] [--debug-file ...]

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
from .model import DEFAULT_TRACE_ROLE, TRACE_ROLE_ENV, normalize_trace_role


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


def _hook_result(args) -> int:
    if getattr(args, "success_json", False):
        print("{}")
    return 0


def _append_private_jsonl(path: str, record: dict) -> None:
    """Append one JSON record while keeping the payload-bearing file user-only."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fd = None
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    finally:
        if fd is not None:
            os.close(fd)


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
        return _hook_result(args)
    # the command-hook adapter: forward the raw payload, never block or break
    try:
        raw = _read_stdin()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            diagnose("payload_parse", harness=args.harness, event=args.event, error=exc)
            return _hook_result(args)
        if not isinstance(payload, dict):
            diagnose("payload_type", harness=args.harness, event=args.event)
            return _hook_result(args)
        event = {
            "v": 1, "harness": args.harness, "event": args.event,
            "captured_at": captured_at, "payload": payload,
            "trace_role": normalize_trace_role(
                os.environ.get(TRACE_ROLE_ENV, DEFAULT_TRACE_ROLE)
            ),
        }
        if not transport.send(event):
            if _ensure_sidecar():
                if not transport.send(event):
                    diagnose("socket_send", harness=args.harness, event=args.event)
            else:
                diagnose("sidecar_unavailable", harness=args.harness, event=args.event)
    except Exception as exc:
        diagnose("hook", harness=args.harness, event=args.event, error=exc)
    return _hook_result(args)


def cmd_sidecar(args) -> int:
    from .config import load_config
    from .emit import WeaveTurnEmitter, serializable_payload
    from .redact import Redactor
    from .sidecar import Sidecar

    cfg = load_config(args.config)
    project = args.project or cfg.project

    debug_file = args.debug_file or os.environ.get("WEAVE_AGENT_ADAPTER_DEBUG_FILE")
    if debug_file:
        def _to_file(payload, project_id, _path=debug_file):
            record = {"project": project_id, "turn": serializable_payload(payload)}
            _append_private_jsonl(_path, record)
        emitter = WeaveTurnEmitter(emit=_to_file)
    else:
        emitter = WeaveTurnEmitter()

    redactor = Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled)
    sc = Sidecar(project, transport.SOCKET_PATH, profiles_dir=args.profiles_dir,
                 idle_s=cfg.idle_shutdown_s, redactor=redactor, session_rate=cfg.session_rate,
                 session_ttl=cfg.session_ttl_s, project_per_repo=cfg.project_per_repo,
                 emitter=emitter)
    signal.signal(signal.SIGTERM, lambda *_: sc.stop())
    signal.signal(signal.SIGINT, lambda *_: sc.stop())
    try:
        sc.serve()
    finally:
        sc.flush_emitter()
    return 0


def cmd_install(args) -> int:
    from .install import install
    from .profile import load_profile

    p = install(args.harness, user=not args.local, profiles_dir=args.profiles_dir,
                path=args.settings_path)
    print(f"registered {args.harness} hooks in {p}")
    note = load_profile(args.harness, args.profiles_dir).registration.get("post_install")
    if note:
        print(note)
    return 0


def cmd_uninstall(args) -> int:
    from .install import uninstall
    p = uninstall(args.harness, user=not args.local, profiles_dir=args.profiles_dir,
                  path=args.settings_path)
    print(f"removed weave-agent-adapter hooks from {p}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weave-agent-adapter")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook")
    h.add_argument("--harness", required=True)
    h.add_argument("--event", required=True)
    h.add_argument("--success-json", action="store_true",
                   help="print an empty JSON object after handling the hook")
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

    args = p.parse_args(argv)
    return args.fn(args)
