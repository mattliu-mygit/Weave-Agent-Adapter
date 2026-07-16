"""Wire event and in-memory turn state shared by the reducer and emitter."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

SUPPORTED_WIRE_VERSION = 1


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


@dataclass
class WireEvent:
    harness: str
    event: str
    captured_at: float
    payload: dict


@dataclass
class Permission:
    decision: Decision = Decision.PENDING
    reason: Optional[str] = None


@dataclass
class Steering:
    kind: SteeringKind
    at: float
    text: Optional[str] = None


@dataclass
class ToolCall:
    correlation_key: str
    tool_name: str
    tool_input: dict
    started_at: float
    agent_id: Optional[str] = None
    permission: Optional[Permission] = None
    status: ToolStatus = ToolStatus.RUNNING
    output: object = None
    error: Optional[str] = None
    ended_at: Optional[float] = None


@dataclass
class Turn:
    started_at: float
    input_text: Optional[str] = None
    output_text: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    turn_id: Optional[str] = None
    tool_calls: dict = field(default_factory=dict)
    steering: list = field(default_factory=list)
    compactions: list = field(default_factory=list)
    subagents: dict = field(default_factory=dict)
    chat_calls: list = field(default_factory=list)
    git_branch: Optional[str] = None
    effort_level: Optional[str] = None
    ended_at: Optional[float] = None
    incomplete: bool = False


@dataclass
class Session:
    session_id: str
    project: str
    last_activity: float
    harness: Optional[str] = None
    transcript: Optional[str] = None
    cwd: Optional[str] = None
    thread_id: Optional[str] = None
    config_version: Optional[str] = None
    current_turn: Optional[Turn] = None
