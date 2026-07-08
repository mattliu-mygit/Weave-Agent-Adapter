# Spec 06: What we send to Weave

Each span (spec 01, layer C) becomes **two requests**: a `call_start` at open and a `call_end` at close, both through the SDK (`create_call` / `finish_call`) in the sidecar. All `inputs`/`output` pass through redaction (spec 07) first.

## Common call shape

`call_start`:
```json
{
  "project_id": "<entity>/<project>",
  "id":        "<span call id>",
  "trace_id":  "<session trace id>",
  "parent_id": "<parent call id | null for session>",
  "op_name":   "weave_agent_adapter.<kind>",
  "started_at":"<ISO8601>",
  "attributes":{ "weave_agent_adapter": { ...static metadata... } },
  "inputs":    { ...span-specific... }
}
```
`call_end`:
```json
{
  "id":       "<span call id>",
  "ended_at": "<ISO8601>",
  "output":   { ...span-specific | null... },
  "exception":"<str | null>",                 // set only on tool_error
  "summary":  { "weave_agent_adapter": { ...computed... } }
}
```

- `attributes` = static facts known at open; `summary` = computed at close (status, duration, decision).
- `started_at`/`ended_at` come from the hooks' `captured_at`, not sidecar time.

## Per-span payloads

| op_name | inputs | output | key attributes / summary |
|---|---|---|---|
| `weave_agent_adapter.session` | `{session_id}` | `{turn_count, status}` | attr: `harness, permission_mode, cwd` |
| `weave_agent_adapter.turn` |   | `{status, tool_count, had_steering}` | attr: `index` |
| `weave_agent_adapter.input` | `{prompt}` (redacted) |   | attr: `kind=input` |
| `weave_agent_adapter.tool.<name>` | `{tool_name, tool_input}` (redacted) | `{...tool_output}` (redacted) or none | summary: `status, duration_s, permission_decision, permission_source, prompt_shown, denial_reason` |
| `weave_agent_adapter.steering` | `{text}` or `{input_diff}` |   | attr: `steering_kind, related_tool` |
| `weave_agent_adapter.stop` |   |   | attr: `kind=stop` |

## Example: an approved Bash tool call

`call_start`:
```json
{
  "project_id": "your-entity/claude-code",
  "id": "c-9f2…", "trace_id": "t-3ab…", "parent_id": "c-turn1…",
  "op_name": "weave_agent_adapter.tool.Bash",
  "started_at": "2026-07-07T19:00:00.000Z",
  "attributes": { "weave_agent_adapter": { "kind": "tool", "harness": "claude-code", "tool_name": "Bash", "session_id": "cafe12…" } },
  "inputs": { "tool_name": "Bash", "tool_input": { "command": "grep -n login auth.py" } }
}
```
`call_end`:
```json
{
  "id": "c-9f2…",
  "ended_at": "2026-07-07T19:00:00.400Z",
  "output": { "stdout": "12: def login(", "exit_code": 0 },
  "exception": null,
  "summary": { "weave_agent_adapter": { "status": "ok", "permission_decision": "allow", "permission_source": "auto", "duration_s": 0.4 } }
}
```

## Example: a rejected Edit

Permission is on the tool call, not a separate span, `call_end` carries the decision:
```json
{ "id": "c-ed1…", "ended_at": "2026-07-07T19:00:01.100Z", "output": null, "exception": null,
  "summary": { "weave_agent_adapter": { "status": "rejected", "permission_decision": "deny",
                                        "denial_reason": "use the logger, not print", "prompt_shown": true } } }
```

## Notes

- **Harness-agnostic namespace:** op names use the tracer namespace `weave_agent_adapter.*` (not the harness's), and the active harness is recorded as the `harness` attribute, so traces from different harnesses share one schema.
- **Redaction** (spec 07): `tool_input`, `tool_output`, and `prompt` are scrubbed before appearing in `inputs`/`output`.
- **Timing rule** (spec 01): long-open spans (session, turn) may `call_start` early so the UI shows them live; short spans emit start+end together at close.
- **OPEN:** exact `tool_output` shape per tool (Bash vs Edit vs Read …) and whether a tool-call id is present, confirmed by M0 capture.
