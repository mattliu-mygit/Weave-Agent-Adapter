# weave-agent-adapter design

## Product contract

The adapter observes coding-agent hook events and turns them into nested
OpenTelemetry traces in W&B Weave without modifying the harness. It records
turns, tools, subagents, permission signals, steering, and hook-captured timing.
Claude Code and Codex use the same command-hook adapter plus declarative TOML
profiles.

The adapter is passive: tracing never approves, rejects, or changes an agent
action. A tracing failure must not fail the harness.

## Architecture

```text
harness hook -> bounded Unix socket -> sidecar reducer
             -> OTel GenAI span tree -> batched OTLP/HTTP -> Weave
```

Hooks are short-lived, standard-library-only processes. They stamp entry time,
read one size- and time-bounded JSON object, send a versioned envelope, and exit
zero with empty stdout. A missing sidecar is started once and retried within a
short deadline. There is no raw capture file, spool, or synchronous network I/O
on the hook path.

The singleton sidecar validates envelopes, loads the named harness profile,
redacts values, and reduces canonical actions into session/turn/tool/subagent
state. Stable tool-call IDs are authoritative; ID-less fallbacks must have one
unambiguous name/input match. Active or pending work prevents idle shutdown.

Each finalized turn becomes one OTel trace. The root is `invoke_agent`, tools
are `execute_tool` children, and subagents are nested `invoke_agent` spans.
Hook timestamps are preserved and the root end encloses every child. Root
attributes include GenAI conventions plus `wandb.thread_id`,
`wandb.is_turn=true`, `input.value`, and `output.value`, allowing Weave to index
the trace in Conversations.

OTel spans are sent directly to Weave with the standard OTLP/HTTP exporter at
`https://trace.wandb.ai/otel/v1/traces`. `wandb.entity` and `wandb.project`
resource attributes route the data. The W&B package supplies authentication and
default-entity discovery; the Weave SDK call API is not another tracing plane.

## Reliability and privacy

Delivery is best-effort. `BatchSpanProcessor` handles asynchronous batching and
normal exporter retry. Provider initialization must succeed before a turn is
marked accepted; shutdown requests a bounded flush. There is no durable outbox,
dead-letter log, replay, or exactly-once guarantee.

Failures are written to a rotating user-only diagnostic log containing phase,
harness/event/project, and exception class only. Raw payloads and exception
messages are excluded.

Redaction occurs before debug or network sinks. Sensitive dictionary keys,
known token shapes, JWTs, AWS access-key IDs, and complete PEM private-key
blocks are replaced. The Unix socket and generated settings files are user-only.

## Extensibility

The core knows canonical actions rather than harness event names. A harness that
passes JSON on stdin needs only a profile describing events, fields, optional
thread derivation/enrichment, registration paths, and its supported hook list.
Missing actions degrade by omission; harness-specific branching does not belong
in the reducer.

## Lifecycle

Closed turns wait briefly for late subagent work, then emit. Sessions without a
session-end event are swept after their TTL and marked incomplete only when work
was actually left open. The sidecar scales to zero after its idle deadline only
when no open or unaccepted turn/tool/subagent work remains.
