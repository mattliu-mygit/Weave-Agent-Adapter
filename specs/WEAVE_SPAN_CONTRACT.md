# Weave agent span contract

## Conversation unit

Each finalized coding-agent turn is passed once to `weave.log_turn`. The stable
thread ID is the Weave conversation ID; the harness session ID is the fallback.
The root is a typed Turn/invoke-agent span, with typed LLM, Tool, and SubAgent
children available to Agents views and Signals.

Root messages preserve the initial prompt, steering messages in order, and the
final assistant reply. Custom `weave_agent_adapter.*` attributes retain the
harness and adapter identity, session ID, working directory, incomplete state,
configuration version, branch, effort, compaction metadata, and filterable
steering, denial, tool-error, and compaction counts when available.
An explicitly observed turn model populates the typed root when no LLM child
provides one; observed native turn IDs and permission modes remain namespaced
attributes rather than being reinterpreted as cross-harness identifiers.

## Child content

LLM spans preserve available model/provider, structured output text and tool
calls, reasoning, finish reasons, response identifiers, cache-aware usage, and
historical timestamps. Per-call inputs remain empty unless a harness exposes
them explicitly; the adapter does not reconstruct them from root messages.

Tool spans preserve redacted arguments and a structured result containing
status, observed subagent ID, output or error, and permission decision/reason.
SubAgent spans preserve type, stable observed ID, and timing.

The public batch API accepts a flat child list. A tool observed inside a
subagent is therefore a typed Tool sibling in the same turn trace, linked by
the agent ID in its result. The adapter does not use private SDK internals or
replay historical work through live context managers to manufacture nesting.

## Harness boundary

Profiles translate native event names and fields into the canonical domain
model. Optional named enrichers may read a harness-specific transcript, but
the reducer and Weave emitter never select behavior by harness name. Adding
another JSON-on-stdin command-hook harness remains a profile change unless it
opts into a separately registered enrichment format.

## Timing and routing

Hook-entry timestamps are authoritative. The root end includes observed child
activity, and child times are clamped into that root window so historical
traces render coherently.

An explicit `entity/project` route is preserved. A bare project is passed to
`weave.init`, which resolves the authenticated default entity. The emitter
initializes the route only when it changes; Weave owns authentication and agent
endpoint routing. Per-repository routing replaces only the project portion and
preserves an explicitly configured entity.

## Failure behavior

Initialization and logging failures drop only that reducer handoff and are
recorded through payload-free diagnostics. A later turn is still attempted.
Shutdown requests a bounded provider flush. The adapter owns no endpoint
override, provider cache, spool, replay, cursor, or retry queue.
