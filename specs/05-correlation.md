# Spec 05 — Tool-call correlation

`PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`/`Failure` arrive as separate hook processes. The sidecar must tie them to **one** `ToolCall` (spec 01) to build the span correctly. This is the framework's #1 risk.

## Resolution chain (first that works wins)

1. **Payload tool-call id** — the profile's `tool_use_id` field. If the harness stamps a stable id on every tool-related event, correlation is exact and trivial. **OPEN: does Claude Code do this? M0 capture answers it.**
2. **Transcript** — parse `transcript_path` (JSONL). The transcript's `tool_use` blocks carry authoritative ids; match the event's `tool_name` + `tool_input` to the block, adopt its id. Costs one file read; robust.
3. **LIFO heuristic** — per `(session_id, tool_name)` stack: `PreToolUse` pushes, `PostToolUse` pops. Last resort only — **fragile under parallel identical tool calls**; used only if 1 and 2 both fail.

The chosen `correlation_key` is stored on the `ToolCall`; all later events for that call resolve to the same key.

## Permission ↔ tool

`PermissionRequest`/`PermissionDenied` attach to the open `ToolCall` via the same key (they carry `tool_name`/`tool_input`, and the id if present). If a permission event can't be matched, it's recorded on the turn as an orphan rather than dropped.

## Edge cases

| Case | Handling |
|---|---|
| `PostToolUse` with no matching open call (missed `Pre`) | create a synthetic `ToolCall` (start ≈ end), mark `partial` |
| Rejected tool (no `Post` follows) | `PermissionDenied` closes it `REJECTED`; a turn/`Stop` boundary flushes any still-open calls |
| Parallel tool calls | fine with id (1) or transcript (2); LIFO (3) may mismatch — logged |
| Duplicate id across turns | key is scoped per session + resolved within the current open set |

## Idempotency

Correlation must be idempotent w.r.t. spool replay (spec 03): resolving the same event twice yields the same key and the same span id, so re-ingested spool lines don't double-count.

## OPEN

- Whether `tool_use_id` exists and on which events (M0) — determines if we ever need tiers 2/3.
- Transcript schema for `tool_use` blocks (confirm field names during M0).
