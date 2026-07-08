# Spec 01 — Data model

Three layers: **(A)** the wire event a hook sends the sidecar, **(B)** the sidecar's in-memory state, **(C)** the Weave call each span becomes. Ties to spec 03 (wire), spec 05 (correlation), spec 06 (weave-mapping).

---

## A. Wire event (hook → sidecar)

The hook stays dumb: it forwards the raw payload plus minimal envelope. The sidecar does all interpretation.

```python
@dataclass
class WireEvent:
    v: int                 # schema version (=1)
    harness: str           # active profile name (from --harness)
    event: str             # native event name (from --event); sidecar maps to canonical
    captured_at: float     # hook wall-clock (epoch s); authoritative span timing
    payload: dict          # raw harness hook stdin, unmodified
    pid: int               # emitting hook pid (debug)
```

- One newline-delimited JSON object per event (see spec 03).
- The hook **parses nothing**: `harness`/`event` come from its launch args, `payload` is forwarded raw. The sidecar extracts `session_id`, `tool_name`, etc. via the profile's `[fields]` (spec 02) — so the hook needs no harness-specific field knowledge.
- `captured_at` is stamped in the hook, not the sidecar — so queued/detached delivery doesn't skew span times.
- Redaction happens in the **sidecar** before the call is sent to Weave, not here (keeps hooks trivial; see spec 07).

---

## B. Sidecar in-memory state

Held by the `Tracer` as `dict[session_id → Session]` — no wrapper class.

```python
@dataclass
class Session:
    session_id: str
    trace_id: str                         # one Weave trace per session
    root_call_id: str                     # the `session` call
    project: str                          # entity/project
    permission_mode: str | None
    cwd: str | None
    started_at: float
    last_activity: float                  # updated each event; drives idle shutdown
    status: SessionStatus                 # OPEN | CLOSED
    current_turn: Turn | None
    turn_count: int

@dataclass
class Turn:
    call_id: str
    index: int                            # 0-based within session
    started_at: float
    open: bool
    input_text: str | None                # prompt (redacted)
    tool_calls: dict[str, ToolCall]       # correlation_key -> ToolCall
    tool_order: list[str]                 # preserves emission order
    steering: list[Steering]
    ended_at: float | None
    status: TurnStatus                    # OPEN | CLOSED

@dataclass
class ToolCall:
    correlation_key: str                  # tool_use_id or fallback (see spec 05)
    call_id: str
    tool_name: str
    tool_input: dict                      # redacted
    started_at: float
    permission: Permission | None
    status: ToolStatus                    # RUNNING | OK | ERROR | REJECTED
    output: Any | None                    # redacted; set on OK
    error: str | None                     # set on ERROR
    ended_at: float | None

@dataclass
class Permission:                          # recorded on the ToolCall, not a span
    requested_at: float | None            # set if a PermissionRequest was seen
    decision: Decision                    # PENDING | ALLOW | DENY
    reason: str | None                    # denial_reason / feedback

@dataclass
class Steering:
    kind: SteeringKind                    # INTERJECTION | DENIAL_FEEDBACK | INPUT_REWRITE
    at: float
    text: str | None                      # interjection / feedback text (redacted)
    input_diff: dict | None               # for INPUT_REWRITE
    related_tool_key: str | None
```

### Enums

| Enum | Values |
|---|---|
| `SessionStatus` | `OPEN`, `CLOSED` |
| `TurnStatus` | `OPEN`, `CLOSED` |
| `ToolStatus` | `RUNNING`, `OK`, `ERROR`, `REJECTED` |
| `Decision` | `PENDING`, `ALLOW`, `DENY` |
| `SteeringKind` | `INTERJECTION`, `DENIAL_FEEDBACK`, `INPUT_REWRITE` |

---

## C. Weave call representation

Every span is a Weave call. IDs are minted at open, stored in state, reused at close.

```python
@dataclass
class WeaveCall:
    id: str                 # unique call id
    trace_id: str           # == session's trace_id
    parent_id: str | None    # parent call id (None only for root session)
    op_name: str            # see spec 06; e.g. "weave_agent_adapter.tool.Bash"
    started_at: float
    ended_at: float | None
    inputs: dict            # span-kind-specific (redacted)
    output: Any | None
    attributes: dict        # namespaced weave_agent_adapter.* metadata
    exception: str | None   # set for ERROR tool calls
```

### Span kinds → source state → parent

| Span (`op_name`) | Built from | Parent |
|---|---|---|
| `weave_agent_adapter.session` | `Session` | — (root) |
| `weave_agent_adapter.turn` | `Turn` | session |
| `weave_agent_adapter.input` | `Turn.input_text` | turn |
| `weave_agent_adapter.tool.<name>` | `ToolCall` (+ `Permission` as attrs) | turn |
| `weave_agent_adapter.steering` | `Steering` | turn |
| `weave_agent_adapter.stop` | `Turn` end | turn |

### Timing rule

OTEL/Weave calls need start+end together, but hook events arrive separately. Rule: **stash `started_at` + ids at open; emit the call (`call_start` then `call_end`) at close**, using stored start + close `captured_at`. Long-open calls (session, turn) may `call_start` early so the UI shows them live — decided in spec 06.

### Key attributes (illustrative; full schema in spec 06)

- session: `permission_mode`, `cwd`, `turn_count`
- tool: `tool_name`, `status`, `duration_s`, `permission_decision`, `permission_source`, `prompt_shown`, `denial_reason`
- steering: `kind`, `related_tool_key`

---

## Identity & lifetime

- **trace_id:** generated at `SessionStart`, one per session, lives in `Session`.
- **call ids:** generated per span at open.
- **correlation_key:** how Pre/Permission/Post find the same `ToolCall` — resolution chain in spec 05; the **OPEN** question (is a stable `tool_use_id` in the payload?) is answered by M0 capture.
- State is a **cache**, fully reconstructible from `transcript_path` — so sidecar crashes are recoverable (see spec 04).
