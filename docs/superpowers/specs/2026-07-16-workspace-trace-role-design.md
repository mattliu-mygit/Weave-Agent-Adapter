# Workspace-Local Trace Role Design

## Goal

Keep the trace-role capability generic and versioned while allowing each local
workspace to select its own role without committing that selection.

## Product boundary

Role definitions, validation, wire propagation, session conflict handling,
Weave attribute emission, tests, and public documentation are repository
behavior and must remain tracked. Only the role selected for a particular
workspace is local state.

## Local selector

A workspace may contain an ignored plain-text file at:

```text
.weave-agent-adapter/trace-role
```

The file contains exactly one supported role name with optional surrounding
whitespace. No TOML schema or new dependency is needed for one value.

The repository ignores `.weave-agent-adapter/` so local selectors, future
workspace-local diagnostics, and other adapter state cannot be committed by
accident. This ignore rule does not affect the existing user-level directory at
`~/.weave-agent-adapter/`.

## Resolution behavior

The hook resolves one role at event capture time in this order:

1. a non-empty `WEAVE_AGENT_TRACE_ROLE` environment value;
2. the nearest `.weave-agent-adapter/trace-role` found while walking from the
   event's working directory toward the filesystem root;
3. `agent_session` when no explicit source exists.

The event working directory comes from the harness profile's canonical `cwd`
field when available and otherwise falls back to the hook process working
directory. The path walk performs only bounded local filesystem reads and no
network or subprocess work.

Every explicit value is normalized through the authoritative role whitelist.
An unknown environment or file value resolves to `other_system`; it does not
fall through to a lower-priority source. An unreadable or missing local file is
treated as absent. An empty local file is also absent.

If events in one session carry conflicting valid roles, the session remains
classified as `other_system`, matching the existing fail-safe behavior.

## Supported roles

- `agent_session`
- `signal_evaluation`
- `judge_evaluation`
- `reflection_evaluation`
- `other_system`

These names remain defined once in the domain model. The local folder contains
only the selected value, not copies of the role definitions.

## Documentation

The README explains the distinction between the committed capability and the
ignored workspace selection, lists supported values, shows how to create the
selector, states precedence, and retains the environment variable for CI or
one-off launches.

## Verification

Tests cover environment precedence, nearest-file selection, parent-directory
lookup, default behavior, invalid and empty values, missing or unreadable
files, canonical harness `cwd` extraction, and the existing session-conflict
fail-safe. Final verification includes the full suite and `git check-ignore`
for `.weave-agent-adapter/trace-role`.

## Cleanup

After the implementation and README are canonical, delete this temporary
design and its implementation plan. Do not commit a real workspace selector.
