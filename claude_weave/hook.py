"""claude-weave hook — capture mode (M0).

Wired to every harness hook event; writes each raw event to disk so we can
confirm payload schemas and tool-call correlation before building the tracer.

Invariants (runs on every tool call): never block (bounded stdin read),
never break (swallow all errors, exit 0), never decide (empty stdout).
"""
from __future__ import annotations

import json
import os
import select
import sys
import time

# Captures land outside the repo so they survive and are easy to find.
CAPTURE_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_WEAVE_CAPTURE_DIR", "~/.claude/claude-weave/capture")
)

# Hard cap on how long we'll wait for stdin, so a hook can never hang a turn.
STDIN_TIMEOUT_S = 0.5


def _read_stdin() -> str:
    """Read the full stdin payload without ever blocking indefinitely."""
    try:
        if sys.stdin is None or sys.stdin.closed:
            return ""
        # If nothing is piped/ready within the timeout, don't block.
        ready, _, _ = select.select([sys.stdin], [], [], STDIN_TIMEOUT_S)
        if not ready:
            return ""
        return sys.stdin.read() or ""
    except Exception:
        return ""


def _detect_event(payload: object, argv: list[str]) -> str:
    """Prefer the payload's own field; fall back to --event, then env."""
    if isinstance(payload, dict) and payload.get("hook_event_name"):
        return str(payload["hook_event_name"])
    for i, a in enumerate(argv):
        if a in ("--event", "-e") and i + 1 < len(argv):
            return argv[i + 1]
    return os.environ.get("CLAUDE_HOOK_EVENT", "unknown")


def _safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)[:120] or "x"


def main() -> int:
    raw = _read_stdin()
    try:
        payload: object = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = None  # parse failed — keep the raw text below

    event = _detect_event(payload, sys.argv[1:])
    session_id = (
        str(payload.get("session_id") or "no-session")
        if isinstance(payload, dict)
        else "no-session"
    )

    record = {
        "captured_at": time.time(),
        "event": event,
        "pid": os.getpid(),
        "argv": sys.argv[1:],
        "stdin_parsed": payload,                       # None if JSON parse failed
        "stdin_raw": raw if payload is None else None,  # kept only on parse failure
    }

    session_dir = os.path.join(CAPTURE_DIR, _safe(session_id))
    os.makedirs(session_dir, exist_ok=True)
    # One file per invocation → no concurrent-append interleaving, exact payloads.
    fname = f"{time.time_ns()}_{os.getpid()}__{_safe(event)}.json"
    tmp = os.path.join(session_dir, fname + ".tmp")
    with open(tmp, "w") as f:
        json.dump(record, f, indent=2, default=str)
    os.replace(tmp, os.path.join(session_dir, fname))  # atomic publish
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Capture must NEVER break a session, no matter what.
        sys.exit(0)
