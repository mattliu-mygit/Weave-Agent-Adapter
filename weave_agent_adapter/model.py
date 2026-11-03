"""The hook-to-sidecar wire event and the trace roles it carries."""
from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_WIRE_VERSION = 1
TRACE_ROLE_ENV = "WEAVE_AGENT_TRACE_ROLE"
DEFAULT_TRACE_ROLE = "agent_session"
OTHER_TRACE_ROLE = "other_system"
TRACE_ROLES = frozenset(
    {
        DEFAULT_TRACE_ROLE,
        "signal_evaluation",
        "judge_evaluation",
        "reflection_evaluation",
        OTHER_TRACE_ROLE,
    }
)


def normalize_trace_role(value: object) -> str:
    return value if isinstance(value, str) and value in TRACE_ROLES else OTHER_TRACE_ROLE


@dataclass
class WireEvent:
    harness: str
    event: str
    captured_at: float
    payload: dict
    trace_role: str = DEFAULT_TRACE_ROLE
