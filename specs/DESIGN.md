# weave-agent-adapter design

## Product contract

The adapter observes coding-agent command hooks and sends each completed turn
to W&B Weave as a typed agent Turn span. It captures prompts, replies, tools,
subagents, permission decisions, steering, compaction, and available model/token
details without modifying the harness. Stable conversation IDs make turns
visible as conversations and eligible for Signals.

Tracing is passive. It never approves, rejects, rewrites, or delays an agent
action beyond a short bounded local handoff. Hook, sidecar, enrichment, and
export failures never fail the harness.

Claude Code, Codex, and Gemini CLI ship as declarative profiles. Another
harness can use the same implementation when it runs a command for lifecycle
events and passes one JSON object on standard input.

## Architecture

```text
harness command hook
  -> bounded Unix-socket handoff
  -> singleton sidecar
  -> profile normalization
  -> session/turn reducer
  -> public weave.log_turn mapping
  -> Weave Agents conversations, spans, and Signals
```

The hook path is standard-library-only. It stamps the entry time, reads one
size- and time-bounded JSON object, sends a versioned envelope, and exits zero.
Stdout is empty by default; a generic command flag emits an empty JSON object
for harnesses that require a JSON acknowledgment. A missing sidecar is started
once and retried within the same short deadline. No hook performs network I/O
or writes raw payloads.

The sidecar owns every dependency-bearing operation: profile loading, field
extraction, redaction, transcript enrichment, state reduction, project
routing, and export. One sidecar multiplexes harnesses and sessions while
keeping their reducer state isolated.

Profiles map native events and common or event-specific payload fields to a
fixed canonical vocabulary. The reducer contains no harness names or native
event names. Harnesses that omit an event or field lose only that detail rather
than requiring alternate reducer paths or rejecting the remaining trace.

## State and lifecycle

The reducer holds only the current mutable turn for each session. Stable
harness tool-call IDs are authoritative; ID-less events match only one
unambiguous running tool with a compatible name and input. Subagents correlate
strictly by agent ID.

A harness turn-end event is the authoritative normal boundary. It closes and
hands the turn to the emitter immediately unless its profile maps a truthy
pending-work field. Pending work leaves the turn mutable until a later clean
turn end; the session TTL remains crash safety if that event never arrives.
After emission, the turn is removed from reducer state regardless of SDK
acceptance, and later events are not attached retroactively. Weave owns
agent-span routing, asynchronous export, batching, and network retry; the
reducer has no partial retry queue.

The sidecar exits after its idle deadline only when no mutable turn remains.
Shutdown finalizes current sessions and requests a bounded exporter flush.

## Reliability and privacy

Delivery is best-effort. There is no spool, outbox, replay, dead-letter log, or
exactly-once guarantee. Losing a sidecar process can lose in-flight state, and
an initialization failure can drop the affected turn. These tradeoffs keep the
hook path bounded and the runtime small.

Redaction occurs before debug or network output. Sensitive keys, common token
shapes, JWTs, AWS access-key IDs, and complete PEM private-key blocks are
replaced. Rotating diagnostics contain phase and type metadata, never payload
values or exception messages. Runtime files are user-only.

## Deliberate boundaries

- Hook and sidecar remain separate for latency and dependency isolation.
- Profile normalization and state reduction remain separate so new command-hook
  harnesses do not add reducer branches.
- Optional named enrichers may understand a native transcript format; the
  reducer and emitter never branch on a harness name. Enrichment is
  best-effort because those formats are not part of this adapter's contract.
- Capabilities are additive: session end, explicit failures, permissions,
  subagents, compaction, model metadata, enrichment, configuration surfaces,
  and pending-work signals are all optional.
- Reduction and Weave mapping remain separate so lifecycle behavior is tested
  independently from SDK objects.
- `weave.log_turn` is the only production tracing plane.
- Configuration resolves one project route before the emitter; the emitter
  does not discover or reinterpret routing.

## Non-goals

- Durable or guaranteed delivery.
- In-process harness instrumentation.
- Hook mechanisms other than JSON on standard input.
- A plugin packaging or marketplace workflow.
- Reconstructing complete reducer state after a crash.
