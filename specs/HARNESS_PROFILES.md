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
- `turn_start`, `turn_update`, `turn_end`
- `tool_pre`, `tool_post`, `tool_error`
- `permission_request`, `permission_denied`
- `subagent_start`, `subagent_stop`
- `compaction`

`turn_update` applies observed metadata without changing the lifecycle. Missing
actions degrade by omission. A missing session end is handled by the session
TTL; a missing tool pre-event produces a completion-only tool record; a missing
subagent start can be materialized from its first interior tool.

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
transcript         = "transcript_path"
model              = "model"
permission_mode    = "permission_mode"
turn_id            = "turn_id"

[event_fields.PostToolUseFailure]
tool_error = "error"

[event_fields.Stop]
pending_work = "background_tasks"

[enrich]
source = "native-transcript-v1"

[registration]
user_path  = "~/.my-harness/hooks.json"
local_path = ".my-harness/hooks.json"
command    = "weave-agent-adapter hook --harness my-harness"
events     = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
```

Field values are dotted paths into the native payload. Optional fields are
simply omitted when unavailable. `[event_fields.<NativeEvent>]` mappings add to
or override `[fields]` for one native event; they select paths only and cannot
run conditions, transforms, defaults, or code. Canonical fields currently used
by the reducer or enrichment path include session, prompt/reply, tool output or
error, permission, subagent identity, transcript, compaction, pending work,
working-directory, model, permission-mode, turn-ID, and effort data.

Capabilities are optional and additive. Profiles may omit session end,
explicit tool failure, permissions, subagents, compaction, model metadata,
transcript enrichment, configuration fingerprinting, or pending-work signals.
Missing capabilities must not prevent the remaining observed turn from being
emitted. A truthy `pending_work` on `turn_end` keeps the turn open; a later
clean `turn_end` closes it, with TTL finalization as fallback.

Optional `[thread]`, `[enrich]`, and `[config_surface]` sections select shipped
interpretation strategies. Unknown strategy names degrade to no enrichment;
they do not create dynamic code-loading behavior. Transcript enrichment is
best-effort and must degrade to the hook-derived turn when a native transcript
is absent, unreadable, or changes shape. Hooks remain authoritative for
lifecycle and tool state.

## Registration

`weave-agent-adapter install --harness NAME` reads `[registration]`, resolves
the installed console script, and atomically merges one command per event into
the harness's JSON settings. Foreign settings and hooks are preserved.
`uninstall` removes only adapter commands.

The shipped harnesses use the standard `{"hooks": {event: [...]}}` settings
shape. A future harness with a different settings format would require an
explicit product decision rather than an unused profile selector.

The registration command may include `--success-json` when a harness requires
a successful hook to print `{}`. This behavior is opt-in and does not require
the hook path to load a profile or branch on a harness name.

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
