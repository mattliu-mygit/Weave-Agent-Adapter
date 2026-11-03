"""Hook dispatcher, sidecar runner, and profile-driven installer CLI.

    weave-agent-adapter hook --harness <name> --event <event>   # per hook event
    weave-agent-adapter sidecar                                  # receives hook events, sends sessions
    weave-agent-adapter evaluations setup|replay|log-transcript|status    # CES session Cases

The hook lazily spawns the sidecar the first time the socket is unreachable, so
running is zero-touch: the session-start event brings the sidecar up.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import transport
from .diagnostics import diagnose, open_diagnostic_stream
from .profile import load_profile
from .trace_role import resolve_trace_role


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


def _event_cwd(harness: str, event: str, payload: dict):
    try:
        return load_profile(harness).extract(payload, event).get("cwd")
    except Exception:
        return None


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
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_stream,
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
            "trace_role": resolve_trace_role(
                _event_cwd(args.harness, args.event, payload)
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
    from .redact import Redactor
    from .sidecar import Sidecar

    cfg = load_config(args.config)
    redactor = Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled)
    session_sender = _session_sender(cfg, redactor, args.profiles_dir)
    sc = Sidecar(transport.SOCKET_PATH, profiles_dir=args.profiles_dir, idle_s=cfg.idle_shutdown_s,
                 redactor=redactor, session_rate=cfg.session_rate, session_ttl=cfg.session_ttl_s,
                 session_sender=session_sender)
    signal.signal(signal.SIGTERM, lambda *_: sc.stop())
    signal.signal(signal.SIGINT, lambda *_: sc.stop())
    try:
        sc.serve()
    finally:
        if session_sender:
            session_sender.close()
    return 0


def _session_sender(cfg, redactor, profiles_dir=None):
    """The CES sender when `[evaluations]` is enabled and setup has run; else None."""
    from . import __version__, dlq
    from .ces import CesClient, api_key_for
    from .ces_writer import CesWriter
    from .config import load_evaluations_state
    from .profile import load_profile
    from .session_sender import SessionSender

    ev = cfg.evaluations
    if not ev.enabled:
        return None
    # Optional feature: nothing here may stop the sidecar from serving or tracing.
    try:
        state = load_evaluations_state()
        if not state:
            return None
        if state.get("project") != ev.project or state.get("name") != ev.name:
            diagnose("ces_config_mismatch")     # rerun `weave-agent-adapter evaluations setup`
            return None
        client = CesClient(ev.base_url, api_key_for(ev.wandb_base_url))
        paths = {}
        for harness in ev.harnesses:
            try:
                paths[harness] = (load_profile(harness, profiles_dir).config_surface or {}).get("paths")
            except Exception:
                pass                            # fingerprinting must never stop sending
        writer = CesWriter(client, state["scope_ref"], state["dataset_id"], rollout_context=ev.rollout_context)
        return SessionSender(writer, dlq.DEFAULT_PATH, redactor, __version__, harnesses=ev.harnesses,
                             config_paths=paths, session_rate=cfg.session_rate)
    except Exception as exc:
        diagnose("ces_config", error=exc)
        return None


def _evaluations_status(path: str) -> int:
    """Print how many turns wait in the dead-letter file, the oldest failure, and why."""
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        entries.append(entry)
                except ValueError:
                    continue            # a line still being appended
    except FileNotFoundError:
        pass
    oldest = min((e["failed_at"] for e in entries if isinstance(e.get("failed_at"), (int, float))), default=None)
    print(json.dumps({
        "dead_letter_file": path,
        "turns_waiting": len(entries),
        "oldest_failure": None if oldest is None else datetime.fromtimestamp(oldest, timezone.utc).isoformat(),
        "error_codes": dict(collections.Counter(str(e.get("error_code")) for e in entries)),
        "sessions_to_rebuild": len({e.get("key") for e in entries if "rebuild" in e}),
    }, indent=2))
    return 0


def cmd_evaluations(args) -> int:
    import urllib.parse

    from . import dlq, records
    from .ces import CesClient, CesError, api_key_for, project_internal_id
    from .ces_writer import CesWriter
    from .config import load_config, load_evaluations_state, save_evaluations_state
    from .redact import Redactor

    if args.action == "status":
        return _evaluations_status(dlq.DEFAULT_PATH)
    cfg = load_config(args.config)
    ev = cfg.evaluations
    try:
        api_key = api_key_for(ev.wandb_base_url)
        client = CesClient(ev.base_url, api_key)
        if args.action == "setup":
            entity, _, project = ev.project.partition("/")
            if not entity or not project:
                print('set [evaluations].project = "entity/project" in config.toml', file=sys.stderr)
                return 2
            scope_ref = project_internal_id(ev.wandb_base_url, api_key, entity, project)
            scope = f"/v1/namespaces/wandb/scopes/{urllib.parse.quote(scope_ref, safe='')}"
            dataset = client.post(f"{scope}/datasets",
                                  {"name": ev.name, "attributes": {"source": "weave-agent-adapter"}},
                                  f"dataset-v1:{ev.project}:{ev.name}")
            client.post(f"{scope}/datasets/{dataset['dataset_id']}/fields", {"fields": records.fields()})
            state = {"project": ev.project, "name": ev.name, "scope_ref": scope_ref,
                     "dataset_id": dataset["dataset_id"]}
            save_evaluations_state(state)
            print(json.dumps(state, indent=2))
            return 0
        state = load_evaluations_state()
        if not state:
            print("run `weave-agent-adapter evaluations setup` first", file=sys.stderr)
            return 2
        if args.action == "log-transcript":
            return _log_transcript(args, cfg, client, state)
        writer = CesWriter(client, state["scope_ref"], state["dataset_id"], rollout_context=ev.rollout_context)
        redactor = Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled)
        try:
            result = dlq.replay(dlq.DEFAULT_PATH, lambda entry: _replay_entry(writer, entry, redactor))
        except dlq.ReplayInProgress as exc:
            print(exc, file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        if result["corrupt"]:
            print(f"{result['corrupt']} unreadable line(s) set aside in {dlq.DEFAULT_PATH}.corrupt",
                  file=sys.stderr)
        return 0 if result["kept"] == 0 else 1
    except CesError as exc:
        print(f"evaluations {args.action} failed: {exc}", file=sys.stderr)
        return 1


def _replay_entry(writer, entry: dict, redactor) -> None:
    """Resend one dead-letter entry. A replayed job has no later job to retry a waiting
    subagent, so its write is final and such a subagent keeps the entry dead-lettered."""
    from . import records
    from .session_sender import READERS, transcript_jobs

    rebuild = entry.get("rebuild")
    if rebuild is None:
        writer.write_turn(entry["job"], final=True)
        return
    ctx = records.TurnContext(**rebuild["ctx"])
    reader = READERS[ctx.harness]
    written = writer.written_turns(ctx.harness, ctx.session_id)
    jobs = transcript_jobs(reader, reader.load(rebuild["transcript"]), ctx, redactor,
                           agents=rebuild["agents"], final=rebuild["final"])
    for _, job in jobs:
        turns = [turn for turn in job["turns"] if turn["turn_id"] not in written]
        if turns:
            writer.write_turn({**job, "turns": turns}, final=True)


def _log_transcript(args, cfg, client, state) -> int:
    """Log one finished session transcript to a named Dataset and print its session IDs."""
    import urllib.parse

    from . import __version__, records
    from .ces_writer import DEFAULT_STATE_DIR, CesWriter
    from .redact import Redactor
    from .session_sender import READERS, transcript_jobs

    if not args.harness or not args.file:
        print("log-transcript needs --harness and --file", file=sys.stderr)
        return 2
    reader = READERS[args.harness]
    t = reader.load(args.file)
    ctx = records.TurnContext(harness=args.harness, session_id=reader.thread_id(t), permission_mode=None,
                              adapter_version=__version__)
    jobs = transcript_jobs(reader, t, ctx, Redactor(deny_keys=cfg.redact_keys, enabled=cfg.redact_enabled))
    if not jobs:
        print(f"{args.file} has no turns", file=sys.stderr)
        return 1
    scope = f"/v1/namespaces/wandb/scopes/{urllib.parse.quote(state['scope_ref'], safe='')}"
    dataset = client.post(f"{scope}/datasets",
                          {"name": args.dataset, "attributes": {"source": "weave-agent-adapter"}},
                          f"dataset-v1:{state['project']}:{args.dataset}")
    dataset_id = dataset["dataset_id"]
    client.post(f"{scope}/datasets/{dataset_id}/fields", {"fields": records.fields()})
    # Each Dataset keeps its own session files, so a session logged here never reuses a live one's IDs.
    writer = CesWriter(client, state["scope_ref"], dataset_id, state_dir=os.path.join(DEFAULT_STATE_DIR, dataset_id),
                       rollout_context=cfg.evaluations.rollout_context)
    for _, job in jobs:
        writer.write_turn(job)
    print(json.dumps(writer.session_ids(args.harness, ctx.session_id)))
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
    s.add_argument("--config")                  # config.toml path (else default)
    s.add_argument("--profiles-dir")
    s.set_defaults(fn=cmd_sidecar)

    e = sub.add_parser("evaluations",
                       help="CES session Cases: set up the Dataset, replay the DLQ, log a transcript")
    e.add_argument("action", choices=("setup", "replay", "log-transcript", "status"))
    e.add_argument("--config")
    e.add_argument("--harness", choices=("codex", "claude-code"))   # log-transcript
    e.add_argument("--file")                                        # log-transcript: the session transcript
    e.add_argument("--dataset", default="ab-arms")                  # log-transcript: Dataset name
    e.set_defaults(fn=cmd_evaluations)

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
