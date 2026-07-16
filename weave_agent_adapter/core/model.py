"""weave-agent-adapter data model (spec 01).

Pure dataclasses + enums: the wire event (layer A) and the sidecar's in-memory
domain state (layer B). The tracer reduces wire events into this model; turn
emitters render finalized turns from it. No behavior here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

SUPPORTED_WIRE_VERSION = 1


# --- Enums ---

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


@dataclass
class ToolCall:
    correlation_key: str                    # tool_use_id or fallback (spec 05)
    tool_name: str
    tool_input: dict                        # redacted
    started_at: float
    agent_id: Optional[str] = None          # set when the tool ran inside a subagent
    permission: Optional[Permission] = None
    status: ToolStatus = ToolStatus.RUNNING
    output: object = None                   # redacted; set on OK
    error: Optional[str] = None             # set on ERROR
    ended_at: Optional[float] = None


@dataclass
class Turn:
    index: int                              # 0-based within session
    started_at: float
    open: bool = True
    input_text: Optional[str] = None        # prompt (redacted)
    output_text: Optional[str] = None       # assistant's final message (redacted)
    tool_calls: dict = field(default_factory=dict)   # correlation_key -> ToolCall
    tool_order: list = field(default_factory=list)   # preserves emission order
    steering: list = field(default_factory=list)     # Steering
    compactions: list = field(default_factory=list)  # (at, trigger)
    subagents: dict = field(default_factory=dict)    # agent_id -> subagent record
    chat_calls: list = field(default_factory=list)   # per-LLM-call records (enrichment)
    git_branch: Optional[str] = None        # from transcript rows (enrichment)
    effort_level: Optional[str] = None      # harness reasoning-effort setting
    ended_at: Optional[float] = None
    incomplete: bool = False                # closed by sweep, not by the harness
    emitted: bool = False                   # handed to turn emitters


@dataclass
class Session:
    session_id: str
    project: str                            # bare name or "entity/project"
    started_at: float
    last_activity: float                    # drives idle shutdown + sweep
    harness: Optional[str] = None           # profile name, e.g. "claude-code"
    permission_mode: Optional[str] = None
    cwd: Optional[str] = None
    transcript: Optional[str] = None        # transcript path; read once for the thread id
    thread_id: Optional[str] = None         # conversation id (fork-stable)
    config_version: Optional[str] = None    # config-surface fingerprint (A/B key)
    current_turn: Optional[Turn] = None
    turn_count: int = 0
