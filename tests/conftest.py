"""Shared test helpers: drive the tracer with wire events, observe emitted turns."""
from __future__ import annotations

from weave_agent_adapter.core.model import WireEvent
from weave_agent_adapter.emit import GenAITurnEmitter
from weave_agent_adapter.profile import load_profile
from weave_agent_adapter.tracer import Tracer

NS = "weave_agent_adapter"


def run(events, harness="claude-code", session_rate=1.0, redactor=None, t0=1000.0,
        project="ent/proj", project_per_repo=False):
    """Feed (native_event, payload) pairs through a tracer and collect the turn
    trees the emitter produced, as (node, project_id) pairs. Each event is
    stamped one second after the last, so durations are stable."""
    turns = []
    emitter = GenAITurnEmitter(default_entity="ent",
                               emit=lambda node, pid: turns.append((node, pid)))
    tr = Tracer(load_profile(harness), project, turn_emitters=[emitter],
                redactor=redactor, session_rate=session_rate,
                project_per_repo=project_per_repo)
    for i, (name, payload) in enumerate(events):
        tr.handle(WireEvent(1, harness, name, t0 + i, payload, 1))
    return tr, turns


def tools_of(node, name=None):
    out = [c for c in node["children"] if c["name"].startswith("execute_tool")]
    if name:
        out = [c for c in out if c["name"] == f"execute_tool {name}"]
    return out


def subagents_of(node, atype=None):
    out = [c for c in node["children"] if c["name"].startswith("invoke_agent")]
    if atype:
        out = [c for c in out if c["name"] == f"invoke_agent {atype}"]
    return out
