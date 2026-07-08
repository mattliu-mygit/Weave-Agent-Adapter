"""weave-agent-adapter data model (spec 01).

Pure dataclasses + enums: the wire event (layer A), the sidecar's in-memory
state (layer B), and the Weave call each span becomes (layer C). No behavior
here — just the shapes the rest of the sidecar builds on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# --- Enums ---

class SessionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class TurnStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ToolStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    REJECTED = "rejected"


class Decision(str, Enum):
    PENDING = "pending"
    ALLOW = "allow"
    DENY = "deny"


class SteeringKind(str, Enum):
    INTERJECTION = "interjection"
    DENIAL_FEEDBACK = "denial_feedback"
    INPUT_REWRITE = "input_rewrite"


# --- Layer A: wire event (hook -> sidecar) ---

@dataclass
class WireEvent:
    v: int
    harness: str
    event: str                 # native event name (from --event)
    captured_at: float         # hook wall-clock; authoritative span timing
    payload: dict              # raw harness hook stdin, unmodified
    pid: int


# --- Layer B: sidecar in-memory state ---

@dataclass
class Permission:
    requested_at: Optional[float] = None    # set if a PermissionRequest was seen
    decision: Decision = Decision.PENDING
    reason: Optional[str] = None            # denial_reason / feedback


@dataclass
class Steering:
    kind: SteeringKind
    at: float
    text: Optional[str] = None              # interjection / feedback (redacted)
    input_diff: Optional[dict] = None       # for INPUT_REWRITE
    related_tool_key: Optional[str] = None


@dataclass
class ToolCall:
    correlation_key: str                    # tool_use_id or fallback (spec 05)
    call_id: str
    tool_name: str
    tool_input: dict                        # redacted
    started_at: float
    permission: Optional[Permission] = None
    status: ToolStatus = ToolStatus.RUNNING
    output: Any = None                      # redacted; set on OK
    error: Optional[str] = None             # set on ERROR
    ended_at: Optional[float] = None


@dataclass
class Turn:
    call_id: str
    index: int                              # 0-based within session
    started_at: float
    open: bool = True
    input_text: Optional[str] = None        # prompt (redacted)
    tool_calls: dict = field(default_factory=dict)   # correlation_key -> ToolCall
    tool_order: list = field(default_factory=list)   # preserves emission order
    steering: list = field(default_factory=list)
    subagents: dict = field(default_factory=dict)    # agent_id -> open subagent span
    ended_at: Optional[float] = None
    status: TurnStatus = TurnStatus.OPEN


@dataclass
class Session:
    session_id: str
    trace_id: str                           # one Weave trace per session
    root_call_id: str                       # the `session` call
    project: str                            # entity/project
    started_at: float
    last_activity: float                    # drives idle shutdown
    permission_mode: Optional[str] = None
    cwd: Optional[str] = None
    status: SessionStatus = SessionStatus.OPEN
    current_turn: Optional[Turn] = None
    turn_count: int = 0


# --- Layer C: Weave call ---

@dataclass
class WeaveCall:
    id: str
    trace_id: str                           # == session's trace_id
    op_name: str                            # e.g. "weave_agent_adapter.tool.Bash"
    started_at: float
    parent_id: Optional[str] = None         # None only for the root session
    ended_at: Optional[float] = None
    inputs: dict = field(default_factory=dict)
    output: Any = None
    attributes: dict = field(default_factory=dict)
    exception: Optional[str] = None         # set for ERROR tool calls
