# weave-agent-adapter: Weave tracing for agent harnesses

> v1 design. Implemented: capture, sidecar + lifecycle, WeaveSink (verified live), redaction, config, sampling, installer, packaging. See §14.

## 1. Principles

- **Normal Weave usage.** `weave.init()` once, warm client for the session → async batching, retry, and native call rendering come from the SDK; redaction and sampling are ours (see §9).
- **Non-intrusive.** The harness is never modified. Hooks are external one-line commands (plugin auto-registers; 0 authored lines). The sidecar is a separate process beside the harness, not inside it.
- **Never block, never break.** Hooks do a µs local write and exit 0; all failure swallowed.
- **Harness-agnostic.** The core runs on a fixed set of **canonical actions**; each harness plugs in via an **adapter** (its hook mechanism) + a declarative **profile** (its event/field/registration mapping), [spec 02](02-harness-profiles.md). Assumes the harness has a hook (or hook-like) system. Command-based hooks reuse one adapter, so most harnesses are profile-only, no code. Two profiles ship, Claude Code and **Codex** (`profiles/codex.toml`); Codex was added with *zero* code changes, the concrete proof of the claim. (Codex also emits `SubagentStart` in practice, which Claude Code documents but doesn't emit in 2.1.201, so the two profiles genuinely exercise different event paths.) Event names below reflect the Claude Code profile.

## 2. Architecture

```
 harness (untouched)
   │  session-start hook ──▶ lazy-spawn / connect ──▶ sidecar (1 warm weave.init, per machine)
   │  every other hook  ──(1 JSON line, local socket)──▶  │  in-mem trace state
   └───────────────────────────────────────────────────  ▼
                                          Weave SDK (async batch / retry) ──▶ Weave
```

- **Hooks = dumb emitters.** Read stdin → forward the raw payload to the sidecar socket → exit 0. No parsing, no SDK, no init, no network. If the socket's missing, append to a spool and move on.
- **Sidecar = the warm client.** `SessionStart` spawns it (detached) or connects if it exists. It calls `weave.init()` once and stays warm until idle, one per machine, multiplexing all sessions (§8), holding correlation state in memory and translating events into Weave calls. This is where Weave's built-ins actually work (they need a live process).
- Why not init in the harness / the hook? We can't run inside the harness's own process, and a hook is a short-lived subprocess (init dies with it in ~2s). The sidecar is "init-at-startup" relocated to a process we control.

## 3. Non-intrusiveness (the constraint)

| Surface | Footprint |
|---|---|
| Harness source | none |
| Adopter code | none, plugin ships `hooks/hooks.json`; one static command per event |
| Runtime footprint | one detached sidecar process (scale-to-zero on idle) + a socket/state dir under `~/.weave-agent-adapter/` |

## 4. Span tree

```
session (root)                 SessionStart → SessionEnd
└── turn                       UserPromptSubmit → Stop
    ├── input                  the prompt
    ├── tool:<name>            PreToolUse → PostToolUse (+ permission attrs)
    ├── steering               mid-turn interjection / input rewrite
    └── stop
```

One trace per session (root call). Nesting via explicit `trace_id`/`parent_id` held in the sidecar, cross-process parenting is first-class in Weave (`call_start` accepts both).

## 5. Event → span

| Event | Sidecar action |
|---|---|
| `SessionStart` | ensure sidecar; open root `session` |
| `UserPromptSubmit` (no open turn) | open `turn`; emit `input` |
| `UserPromptSubmit` (turn open) | emit `steering` (interjection) |
| `PreToolUse` | open `tool:<name>` under turn |
| `PermissionRequest` | record on tool (prompt shown) |
| `PermissionDenied` | close tool `rejected` (deny attrs) |
| `PostToolUse` / `…Failure` | close tool ok/error (allow attrs) |
| `Stop` | emit `stop`; close `turn` |
| `SessionEnd` | close `session`; flush |

_Native event names above are the Claude Code profile ([profiles/claude-code.toml](../weave_agent_adapter/profiles/claude-code.toml)); other harnesses map their own via [spec 02](02-harness-profiles.md)._

## 6. Correlation (in-memory in sidecar)

Match `Pre ↔ Permission ↔ Post` to one tool call: primary = `tool_use_id` (**M0-confirmed** as a stable per-call id shared Pre/Post, real values like `toolu_01…`); last resort = last still-running tool (LIFO).

## 7. Approval / steering / rejection

- **Approval:** inferred, a tool reaching `PostToolUse` was allowed; recorded as tool attributes (`permission_decision=allow`, `permission_source`, `prompt_shown`), not a span.
- **Rejection:** `PermissionDenied` → tool closed `rejected` with `permission_decision=deny` + `denial_reason`.
- **Steering:** (a) `UserPromptSubmit` before `Stop`; (b) denial with feedback; (c) `PreToolUse` `updatedInput`. Each = a `steering` span.

## 8. Concurrency, singleton & isolation

One sidecar (fixed socket + `flock`) multiplexes all sessions: `dict[session_id → trace]`, each its own `trace_id`. `weave.init()` once + concurrent `create_call` from threads is Weave's intended, thread-safe path (`ContextVar` stacks; non-blocking deferred sends). Isolation: per-session state, per-event `try/except`. Global client is single-project; concurrent different projects → hold `dict[project → client]`.

## 9. Reliability & Weave built-ins

What the SDK gives us on the low-level (`call_start`/`call_end`) path: **native call rendering** (spans land in Weave's call model, not raw OTel JSON), **async batching**, and **retry** with backoff (`retry_max_attempts=3`). Items that still fail are appended to a **disk dead-letter log** (`enable_disk_fallback`, on by default), best-effort, *not* a replay-on-restart WAL. **Redaction** and **sampling** are ours (`redact.py`; a per-session hash in the tracer): the SDK's `redact_keys`/`tracing_sample_rate` only apply to the `@weave.op` path, which we bypass because the low-level call API is the only surface that accepts explicit timestamps (§ timing). Autopatching is disabled (we trace no LLM SDKs).

## 10. Latency & safety

- On-path per hook ≈ local socket write (µs). Weave I/O is off-path in the sidecar.
- Hooks always exit 0, empty stdout (no decision), hard timeout on the write.
- Sidecar `flush()` on shutdown; `weave.init` runs once with autopatch disabled (`WEAVE_IMPLICITLY_PATCH_INTEGRATIONS=false`), the cost amortized over the sidecar's life.

## 11. Lifecycle

- **Spawn:** `SessionStart` (lazy, singleton). **Shutdown:** idle ~60–120s after last session / empty queue, post-flush.
- **Crash / no `SessionEnd`:** a periodic sweep (`session_ttl_s`) finalizes sessions idle past the TTL, frees memory *and* closes the trace (marked `incomplete`) rather than leaving it dangling; the sidecar also finalizes any still-open sessions on shutdown. Undelivered sends are retried in-process; whatever fails lands in the SDK's disk dead-letter log, no automatic replay.
- **Subagents:** nested under an `agent.<type>` span keyed by `agent_id`, with interior tools re-parented under it. Opened at `SubagentStart` if the harness emits it (Codex), else lazily on the first interior tool (Claude Code 2.1.201 documents `SubagentStart` but doesn't emit it); closed at `SubagentStop`. **Compaction:** `PreCompact` annotates the session with the trigger.

## 12. Integration (zero authored lines)

One static command per event, `weave-agent-adapter hook --harness <h> --event <e>`, where `--event` comes from the profile's `[registration]`; the sidecar maps the native event to a canonical action (spec 02). Ladder: **plugin** (0 lines) → `weave-agent-adapter install` (1 command) → paste generated block (~9 entries). No all-events wildcard, so entries are generated per event.

## 13. Non-goals (v1)

- **Custom durability / crash-recovery layer**, v1 is best-effort: SDK batching + retry, with a disk dead-letter log for drops. No bespoke spool/replay, and no automatic replay of the dead-letter log.

## 14. Milestones

- **M0, Capture:** ✅ verified against a real Claude Code 2.1.201 session, `tool_use_id` is a stable per-call id shared Pre/Post (correlation confirmed); `session_id`/`tool_name`/`tool_response`/`cwd`/`permission_mode` as expected; `SubagentStop` carries `agent_type`+`agent_id`, and subagent-interior tools carry the subagent's `agent_id` (→ future: nest them under the subagent). Still provisional: `PermissionRequest`/`PermissionDenied` payloads (bypassed in headless) and `PreCompact.trigger`.
- **M1, Sidecar + core tree:** ✅ socket, warm `weave.init`, session/turn/tool spans, cross-process nesting, lazy-spawn/singleton/idle. WeaveSink verified live.
- **M2, Permission/approval/rejection/steering:** ✅ (as tool attributes + steering spans).
- **M3, Redaction, sampling, config:** ✅
- **M4, Subagents, compaction, hardening:** ✅ canonical `subagent_start`/`subagent_stop` (stop-only harnesses annotate) + `compaction`; Claude Code maps `SubagentStop`/`PreCompact`.
- **M5, Packaging + installer:** ✅ pyproject + `install`/`uninstall` + `plugin` (ships `.claude-plugin/plugin.json` + `hooks/hooks.json`, in [plugin/claude-code](../plugin/claude-code)).

## 15. Prior art & the SDK-vs-OTLP decision (verified)

Two projects overlap: **`wandb/weave-claude-code`** (official; daemon + OTLP to Weave, Claude-only, TS, no redaction/sampling) and **`o11y-dev/opentelemetry-hooks`** (harness-agnostic, OTLP, Python). Weave also ingests OTLP directly (`trace.wandb.ai/otel/v1/traces`).

We keep the **Weave-SDK low-level path** (`call_start`/`call_end`) rather than OTLP, verified live by emitting the same session both ways:

- **Timing forces it.** `create_call`/`finish_call` (0.52.17) take no timestamp args → ingest wall-clock. Only the low-level API accepts our hook-stamped `started_at`/`ended_at`. Redaction/sampling live *only* on the high-level `@weave.op` path, so going low-level means we own them (that's why `redact.py` exists, not reinvention).
- **What the SDK actually buys us** (low-level): native call rendering, async batching, retry, disk dead-letter. Not redaction/sampling/cost/feedback.
- **OTLP is genuinely close when tuned** (OpenInference `input.value`/`output.value`, `wandb.display_name`, `gen_ai.usage.*`): identical nesting, clean names, token/cost, feedback, monitors all work. Residual OTLP gaps we confirmed: inputs nest under an `input.value` wrapper (a literal dotted key → monitor filters need `input\.value.…` or silently return 0); permission lands inside the raw `otel_span` (events/status) not as a top-level attribute; hashed `op_name`; and **playground/chat replay is effectively SDK-only**.
- **Net:** for a permission/steering tracer, first-class permission attributes + clean queryability + playground are the SDK's real edge; operational monitoring/cost/feedback are a wash. Revisit OTLP if broad harness reach outranks rich Weave rendering.
