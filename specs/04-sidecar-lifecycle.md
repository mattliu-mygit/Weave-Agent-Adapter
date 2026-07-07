# Spec 04 — Sidecar lifecycle

One long-lived process per machine, hosting the warm Weave client (spec 01, layer B). Lazy-spawned, singleton, self-terminating.

## Spawn (lazy, race-safe)

Any hook that can't reach the socket may spawn it; in practice the session-start hook does. Sequence:

1. Try to `connect` the socket. Success → send + exit.
2. Refused → acquire an exclusive `flock` on `~/.claude/claude-weave/sidecar.lock`.
   - **Won the lock:** double-fork + `setsid` to detach, `exec` the sidecar, poll until the socket accepts (bounded ~2 s), release lock, then send.
   - **Lost the lock:** another hook is spawning — poll-connect with backoff; if still down past the deadline, spool (spec 03) and exit.

Binding the fixed socket path is itself mutually exclusive, so at most one sidecar ever runs. The lock only avoids a thundering herd of spawns.

## Singleton & multiplexing

- Holds `sessions: dict[session_id → Session]`; each session = its own `trace_id`/root call.
- One `weave.init()` client (per project; `dict[project → client]` only if sessions target different projects).
- Init tuned once: explicit `entity/project`, `ensure_project_exists=False`, `WEAVE_IMPLICITLY_PATCH_INTEGRATIONS=false`.

## Idle shutdown ("serverless" feel)

Timer-driven. Exit when **no active sessions** AND the queue is drained, for `sidecar.idle_shutdown_s` (default 120 s). On shutdown: `client.flush()`, close the socket, remove the lock. It reappears on the next session-start hook.

## Crash recovery (best-effort, v1)

State is an in-memory cache; a crash loses in-flight session state, which is acceptable for v1:

- `WEAVE_ENABLE_WAL=true` covers **delivery** durability — queued sends replay on restart.
- Orphaned open calls from a crashed instance are left as-is (they show open in Weave); no custom finalize layer in v1.

## Periodic sweep

While running, a timer drops sessions idle beyond TTL (no `SessionEnd` arrived — e.g. the harness was killed) so memory doesn't leak; their Weave calls are left as-is (best-effort).

## Signals

`SIGTERM`/`SIGINT` → flush, finalize open calls as `incomplete`, exit 0.

## OPEN

- Exact TTLs (idle-shutdown vs. idle-session drop) — tune after real runs.
