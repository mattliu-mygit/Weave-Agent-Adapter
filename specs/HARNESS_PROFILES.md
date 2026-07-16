# Harness profile contract

## Purpose

A profile translates one command-hook harness into the adapter's canonical
event model. Adding a harness should require a TOML profile, not reducer or
installer branches.

The supported harness contract is intentionally narrow: the harness runs a
command for lifecycle events and passes one JSON object on standard input.
Other delivery mechanisms are outside the current product.

## Canonical actions

Profiles may map native events to these actions:

- `session_start`, `session_end`
- `turn_start`, `turn_end`
- `tool_pre`, `tool_post`, `tool_error`
- `permission_request`, `permission_denied`
- `subagent_start`, `subagent_stop`
- `compaction`

Missing actions degrade by omission. A missing session end is handled by the
session TTL; a missing tool pre-event produces a completion-only tool record;
a missing subagent start can be materialized from its first interior tool.

## Profile shape

```toml
[harness]
name = "my-harness"

[events]
SessionStart      = "session_start"
UserPromptSubmit  = "turn_start"
PreToolUse        = "tool_pre"
PostToolUse       = "tool_post"
Stop              = "turn_end"

[fields]
session_id        = "session_id"
prompt            = "prompt"
assistant_message = "last_assistant_message"
tool_name          = "tool_name"
tool_input         = "tool_input"
tool_output        = "tool_response"
tool_use_id        = "tool_use_id"
cwd                = "cwd"
model              = "model"
permission_mode    = "permission_mode"
turn_id            = "turn_id"

[registration]
user_path  = "~/.my-harness/hooks.json"
local_path = ".my-harness/hooks.json"
command    = "weave-agent-adapter hook --harness my-harness"
events     = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
```

Field values are dotted paths into the native payload. Optional fields are
simply omitted when unavailable. Canonical fields currently used by the
reducer or enrichment path include session, prompt/reply, tool, permission,
subagent, transcript, compaction, working-directory, model, permission-mode,
turn-ID, and effort data.

Optional `[thread]`, `[enrich]`, and `[config_surface]` sections select shipped
interpretation strategies. Unknown strategy names degrade to no enrichment;
they do not create dynamic code-loading behavior.

## Registration

`weave-agent-adapter install --harness NAME` reads `[registration]`, resolves
the installed console script, and atomically merges one command per event into
the harness's JSON settings. Foreign settings and hooks are preserved.
`uninstall` removes only adapter commands.

Both shipped harnesses use the standard `{"hooks": {event: [...]}}` settings
shape. A future harness with a different settings format would require an
explicit product decision rather than an unused profile selector.

A profile may declare a `post_install` note for a required activation step,
such as reviewing a newly registered hook in the harness trust UI. The CLI
prints the note without adding harness-specific installer behavior.

## Adding a harness

1. Verify its event names, JSON payload fields, settings location, and stable
   tool-call identifier from authoritative documentation or a private capture.
2. Add the profile under `weave_agent_adapter/profiles/`.
3. Add reducer integration tests covering the events the harness actually
   emits and installer tests covering its registration path.
4. Document genuine degradation where the harness lacks an action.

Raw payload capture is not a shipped runtime feature because it conflicts with
the adapter's privacy boundary.
