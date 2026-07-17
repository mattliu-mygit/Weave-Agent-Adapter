# Lean harness extensibility design

## Goal

Make standard JSON-on-stdin harnesses profile-only integrations while handling
real harness differences with the smallest reusable mechanisms that current
Claude Code, Codex, and Gemini CLI behavior require.

The implementation must preserve one harness-neutral reducer and one Weave
emitter. It must not introduce a plugin framework, adapter class hierarchy,
dynamic imports, a profile expression language, or abstractions justified only
by hypothetical harnesses.

## Product behavior

Profiles continue to map native event names and payload fields into the fixed
canonical event model. A profile may additionally provide field mappings for a
specific native event when that event uses a different payload field for the
same canonical concept.

Harness capabilities are additive and optional. A profile may omit session
end, explicit tool failure, permissions, subagents, compaction, model metadata,
transcript enrichment, configuration fingerprinting, or pending-work signals.
Missing capabilities remove only the corresponding detail; they never prevent
other events from producing a turn.

For Claude Code:

- `PermissionDenied.reason` becomes the canonical denial reason.
- `PostToolUseFailure.error` becomes the canonical tool error.
- documented `model` and `permission_mode` values are retained when present;
- a Stop event with nonempty `background_tasks` does not finalize the turn;
- a later Stop with no pending background work finalizes normally;
- the existing session TTL remains the fallback when no later Stop arrives.

Claude Code only emits `PermissionDenied` for auto-mode classifier denials.
Manual permission-dialog denials therefore remain outside the hook-derived
contract unless a future reliable source makes them observable.

For Gemini CLI:

- `BeforeAgent` and `AfterAgent` provide turn boundaries and final text;
- `BeforeTool` and `AfterTool` provide tool lifecycle and results;
- an error nested in `AfterTool.tool_response.error` marks the tool as failed;
- `BeforeModel.llm_request.model` updates the current turn's model when present;
- `PreCompress` records compaction and SessionStart/SessionEnd retain lifecycle;
- missing permission and subagent events simply omit those spans and counters;
- absent transcript enrichment still produces the fallback final-response LLM
  child from `AfterAgent.prompt_response`.

Gemini's streaming `AfterModel` hooks and preview subagent system are not used
in this iteration. They would add substantial state and event volume without
being required for useful turn, model, tool, and response traces.

## Architecture

### Profile normalization

The existing common `[fields]` table remains authoritative for fields shared
across events. A new `[event_fields.<NativeEvent>]` table may override or add
canonical mappings for one event. `Profile.extract` receives the native event,
applies common mappings first, then applies that event's mappings.

This is intentionally limited to field selection. It does not perform
conditions, transforms, defaulting, or arbitrary code execution.

An event may map to a canonical update action that has no lifecycle transition.
This lets a model hook add observed model metadata to the current turn without
introducing model-hook behavior into the reducer.

### Canonical lifecycle

`pending_work` is a canonical optional field whose value is treated only for
truthiness. A turn-end event with pending work records the latest observed turn
fields but leaves the turn mutable. A later turn-end without pending work closes
and emits it. Harnesses without this field retain current behavior.

This is reducer behavior, not a Claude branch: another harness can map its own
pending-work payload to the same canonical field.

### Existing named strategies

Transcript enrichment remains selected through the existing closed registry.
No new general strategy protocol is introduced. A new named strategy should be
added only when a second real behavior cannot be represented through event and
field normalization.

### Registration and hook output

Claude Code, Codex, and Gemini CLI all use the existing nested JSON hook
settings shape, so the current installer remains the only registration
implementation. The Gemini profile uses its documented user and workspace
settings paths.

The hook command gains a profile-declared command flag that writes `{}` on
successful completion for harnesses that require JSON stdout. The hook does not
load a profile or branch on a harness name. Existing profiles retain empty
stdout, preserving their current behavior.

### Canonical model cleanup

State that cannot reach the current Weave span contract should not be collected
speculatively. Subagent identity, type, and timing remain; unused subagent final
output is removed from the profile-to-model path. Other internal model cleanup
is out of scope unless directly exposed by these changes.

## Failure behavior

Unknown native events and absent optional fields continue to degrade by
omission. Event-specific field mappings for an event not present in `[events]`
are inert. Malformed profile structures fail during profile loading rather than
changing reducer behavior at runtime.

An event received without a session identifier is ignored as it is today. An
unknown harness profile is diagnosed and drops only that event. Unsupported
optional actions and fields never stop the sidecar or invalidate an otherwise
usable turn.

The hook remains passive, bounded, payload-redacting, and exit-zero. No change
may add network work or dependency-heavy imports to the hook path.

## Testing

Focused tests use payloads matching the current official Claude Code hook
schemas and prove:

- common and event-specific mappings merge correctly;
- event-specific mappings override a common mapping when both exist;
- denial reasons and tool failure messages reach the canonical model;
- model and permission mode are retained;
- pending background work defers emission until a clean Stop;
- missing pending-work fields preserve immediate finalization;
- Gemini's documented session, agent, model, tool, and compression payloads
  reduce into the useful subset of the canonical model;
- an embedded Gemini tool error becomes an error without a separate failure
  event;
- Gemini hook commands return `{}` while Claude and Codex retain empty stdout;
- every registered Claude, Codex, and Gemini hook reaches the sidecar;
- profiles that omit permissions, subagents, compaction, model data, session
  end, or enrichment still emit the remaining observed turn data;
- third-party profiles using only the existing common fields remain compatible.

The complete test suite, package compilation, diff checks, and repository status
are verified before the final commit and push.

## Documentation and cleanup

The durable profile and lifecycle contracts are folded into `specs/DESIGN.md`
and `specs/HARNESS_PROFILES.md`. Claude limitations and the supported Gemini
feature subset are stated precisely in the README. This temporary design and
its implementation plan are deleted after the canonical documents describe the
final implementation.
