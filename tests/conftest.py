"""Shared test helpers: drive the tracer with wire events, observe emitted turns."""
from __future__ import annotations

from weave_agent_adapter.model import WireEvent
from weave_agent_adapter.profile import load_profile
from weave_agent_adapter.tracer import Tracer

NS = "weave_agent_adapter"


class CapturingEmitter:
    def __init__(self, finalized):
        self.finalized = finalized

    def emit_turn(self, turn, session):
        self.finalized.append((turn, session))
        return True


def run(events, harness="claude-code", session_rate=1.0, redactor=None, t0=1000.0,
        project="ent/proj", project_per_repo=False, trace_role="agent_session"):
    """Feed native events through a tracer and collect finalized domain turns."""
    finalized = []
    tr = Tracer(load_profile(harness), project, emitter=CapturingEmitter(finalized),
                redactor=redactor, session_rate=session_rate,
                project_per_repo=project_per_repo)
    for i, (name, payload) in enumerate(events):
        tr.handle(WireEvent(harness, name, t0 + i, payload, trace_role=trace_role))
    return tr, finalized


def tools_of(turn, name=None):
    out = list(turn.tool_calls.values())
    if name:
        out = [tool for tool in out if tool.tool_name == name]
    return out


def subagents_of(turn, atype=None):
    out = list(turn.subagents.values())
    if atype:
        out = [agent for agent in out if agent["type"] == atype]
    return out
