# claude-weave: Weave tracing for agent harnesses (sidecar-primary)

> Proposal — awaiting approval. No code beyond M0 capture.

## 1. Principles

- **Normal Weave usage.** `weave.init()` once, warm client for the session → WAL, batching, retry, redaction, sampling all native.
- **Non-intrusive.** The harness is never modified. Hooks are external one-line commands (plugin auto-registers; 0 authored lines). The sidecar is a separate process beside the harness, not inside it.
- **Never block, never break.** Hooks do a µs local write and exit 0; all failure swallowed.
- **Harness-agnostic.** The core runs on canonical events (session/turn/tool/permission); a declarative per-harness **profile** ([specs/02](specs/02-harness-profiles.md)) maps a harness's native events, payload fields, and hook registration. New harness = new profile, no code. Claude Code is the first profile; event names below reflect it.

## 2. Architecture

```
 harness (untouched)
   │  session-start hook ──▶ lazy-spawn / connect ──▶ sidecar (1 warm weave.init, per machine)
   │  every other hook  ──(1 JSON line, local socket)──▶  │  in-mem trace state
   └───────────────────────────────────────────────────  ▼
                                          Weave SDK (async/WAL/retry/redact) ──▶ Weave
```

- **Hooks = dumb emitters.** Parse stdin → write one line to the sidecar socket → exit 0. No SDK, no init, no network. If the socket's missing, append to a spool and move on.
- **Sidecar = the warm client.** `SessionStart` spawns it (detached) or connects if it exists. It calls `weave.init()` once and lives for the session, holding correlation state in memory and translating events into Weave calls. This is where Weave's built-ins actually work (they need a live process).
- Why not init in the harness / the hook? We can't run inside the harness's own process, and a hook is a short-lived subprocess (init dies with it in ~2s). The sidecar is "init-at-startup" relocated to a process we control.

## 3. Non-intrusiveness (the constraint)

| Surface | Footprint |
|---|---|
| Harness source | none |
| Adopter code | none — plugin ships `hooks/hooks.json`; one static command per event |
| Runtime footprint | one detached sidecar process (scale-to-zero on idle) + a socket/state dir under `~/.claude/claude-weave/` |

## 4. Span tree

```
session (root)                 SessionStart → SessionEnd
└── turn                       UserPromptSubmit → Stop
    ├── input                  the prompt
    ├── tool:<name>            PreToolUse → PostToolUse
    │   └── permission         PermissionRequest → allow|deny
    ├── steering               mid-turn interjection / input rewrite
    └── stop
```

One trace per session (root call). Nesting via explicit `trace_id`/`parent_id` held in the sidecar — cross-process parenting is first-class in Weave (`call_start` accepts both).

## 5. Event → span

| Event | Sidecar action |
|---|---|
| `SessionStart` | ensure sidecar; open root `session` |
| `UserPromptSubmit` (no open turn) | open `turn`; emit `input` |
| `UserPromptSubmit` (turn open) | emit `steering` (interjection) |
| `PreToolUse` | open `tool:<name>` under turn |
| `PermissionRequest` | open `permission` under tool |
| `PermissionDenied` | close permission `deny`; mark tool rejected |
| `PostToolUse` / `…Failure` | close permission `allow`; close tool ok/error |
| `Stop` | emit `stop`; close `turn` |
| `SessionEnd` | close `session`; flush |

_Native event names above are the Claude Code profile ([profiles/claude-code.toml](profiles/claude-code.toml)); other harnesses map their own via [spec 02](specs/02-harness-profiles.md)._

## 6. Correlation (in-memory in sidecar)

Match `Pre ↔ Permission ↔ Post` to one tool call: primary = a tool-call id from the payload **if it exists** (open question — **M0 confirms**); fallback = `transcript_path`; last resort = `(tool_name, hash(input))` LIFO.

## 7. Approval / steering / rejection

- **Approval:** inferred — tool reaching `PostToolUse` closes its permission span as `allow` (source: user/hook/auto).
- **Rejection:** `PermissionDenied` → permission `deny` + reason; tool rejected.
- **Steering:** (a) `UserPromptSubmit` before `Stop`; (b) denial with feedback; (c) `PreToolUse` `updatedInput`. Each = a `steering` span.

## 8. Concurrency, singleton & isolation

One sidecar (fixed socket + `flock`) multiplexes all sessions: `dict[session_id → trace]`, each its own `trace_id`. `weave.init()` once + concurrent `create_call` from threads is Weave's intended, thread-safe path (`ContextVar` stacks; non-blocking deferred sends). Isolation: per-session state, per-event `try/except`. Global client is single-project; concurrent different projects → hold `dict[project → client]`.

## 9. Reliability & Weave built-ins

Because the sidecar runs the SDK, we get for free: **WAL** (`WEAVE_ENABLE_WAL=true`, crash-safe restart), **async batching + retry**, **redaction** (`redact_pii`, `redact_keys`), **sampling** (root-only `tracing_sample_rate`). Best-effort tracing without hand-rolling any of it.

## 10. Latency & safety

- On-path per hook ≈ local socket write (µs). Weave I/O is off-path in the sidecar.
- Hooks always exit 0, empty stdout (no decision), hard timeout on the write.
- Sidecar `flush()` on shutdown; init tuned once (explicit `entity/project`, `ensure_project_exists=False`, `WEAVE_IMPLICITLY_PATCH_INTEGRATIONS=false`).

## 11. Lifecycle

- **Spawn:** `SessionStart` (lazy, singleton). **Shutdown:** idle ~60–120s after last session / empty queue, post-flush.
- **Crash / no `SessionEnd`:** sweep sessions idle > TTL, finalize open calls `incomplete`. `WEAVE_ENABLE_WAL` makes queued sends crash-safe.
- **Subagents:** `SubagentStart/Stop` nest via `agent_id`. **Compaction:** `PreCompact/PostCompact` annotate.

## 12. Integration (zero authored lines)

One static command (`claude-weave hook`) per event; dispatcher branches on `hook_event_name`. Ladder: **plugin** (0 lines) → `claude-weave install` (1 command) → paste generated block (~9 entries). No all-events wildcard, so entries are generated per event.

## 13. Fallback: OTLP-direct (daemonless)

If a sidecar is unwanted, hooks POST OTLP protobuf straight to Weave (double-fork + detach, ~ms on-path). Loses SDK WAL/batching/retry/redaction; add a mini-spool for best-effort. Same span model + dispatcher — a config toggle.

## 14. Milestones

- **M0 — Capture (in progress):** `hook.py` capture dispatcher is written; the payload inspector (`tools/inspect_capture.py`) and a real-session run to confirm the schema + tool-call-id correlation are still pending.
- **M1 — Sidecar + core tree:** socket, warm `weave.init`, session/turn/tool spans, cross-process nesting.
- **M2 — Permission/approval/rejection/steering.**
- **M3 — Redaction, sampling, WAL, config.**
- **M4 — Crash reconciliation, subagents, compaction; OTLP fallback mode.**
- **M5 — Plugin + pip packaging.**
