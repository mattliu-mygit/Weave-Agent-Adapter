# Spec 03 — Hook → sidecar: dispatcher & wire protocol

The single command wired to every event, and how it hands the event to the sidecar. The hook is dumb: it forwards the raw payload and lets the sidecar normalize via the profile (spec 02).

## Entrypoint

```
claude-weave hook --harness NAME --event NAME
```

Both args come from the profile's `[registration]` (spec 02) — so the hook never parses the payload to learn its harness or event. The same static command backs every event.

## What the hook does

1. Read stdin, bounded (0.5 s cap — never hang a turn).
2. Wrap it as a `WireEvent` (spec 01): `{v, harness, event, captured_at, pid, payload}` — `payload` is the raw stdin verbatim; `captured_at` is stamped here (authoritative timing).
3. **Session-start event only:** ensure the sidecar is up (spec 04).
4. Send to the sidecar socket; on failure, best-effort spool.
5. Exit 0.

It parses **nothing** out of the payload — `session_id`, `tool_name`, etc. are extracted by the sidecar via the profile's `[fields]`.

## Invariants (runs on every tool call)

- **Never block:** bounded stdin read, bounded socket write, no synchronous network. The hook never calls Weave — deferral is the sidecar's job (spec 04).
- **Never break:** catch-all → always `exit 0`.
- **Never decide:** stdout left empty. We're a passive observer; even on `PreToolUse` we never emit a `permissionDecision`.

## Transport

- **Unix domain socket**, `SOCK_STREAM`, at `~/.claude/claude-weave/sidecar.sock` (overridable). Local only, mode `0600`.
- STREAM (not DGRAM) so large payloads (big tool outputs) aren't capped by datagram limits.

## Framing

Newline-delimited JSON — one `WireEvent` per line. The hook `connect → write one line → close`; it does not wait for a reply (fire-and-forget).

```json
{"v":1,"harness":"claude-code","event":"PreToolUse","captured_at":1751914800.12,"pid":41234,"payload":{ ...raw hook stdin... }}
```

## Failure handling (best-effort)

- The **session-start hook waits** until the socket is accepting before returning (spec 04), so later hooks in the session find the sidecar up.
- If a connect/write still fails (e.g. sidecar crashed mid-session) → swallow, exit 0. v1 is best-effort: dropping the occasional event is acceptable.
- **Optional thin spool** (`~/.claude/claude-weave/spool/<session>.jsonl`) drained by the sidecar on next contact (idempotent by span id). Kept minimal — not a durability layer.

## Versioning

`v` is the schema version. The sidecar accepts known versions and logs+drops unknown ones (never crashes).

## OPEN

- Whether `--event` can be dropped once payloads reliably carry the native event name (M0). Even then, passing it via `[registration]` keeps the hook parse-free, which is preferable.
