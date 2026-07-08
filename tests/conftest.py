"""Shared test helpers: drive the tracer with wire events, inspect emissions."""
from __future__ import annotations

from weave_agent_adapter.core.model import WireEvent
from weave_agent_adapter.profile import load_profile
from weave_agent_adapter.sinks.recording import RecordingSink
from weave_agent_adapter.tracer import Tracer

NS = "weave_agent_adapter"


def run(events, session_rate=1.0, redactor=None, t0=1000.0, harness="claude-code"):
    """Feed (native_event, payload) pairs through a tracer for `harness`.

    Each event is stamped one second after the last, so durations are stable.
    Returns (tracer, sink).
    """
    tr = Tracer(load_profile(harness), "e/p", RecordingSink(),
                redactor=redactor, session_rate=session_rate)
    for i, (name, payload) in enumerate(events):
        tr.handle(WireEvent(v=1, harness=harness, event=name,
                            captured_at=t0 + i, payload=payload, pid=1))
    return tr, tr.sink


def starts(sink):
    return [c for k, c in sink.events if k == "start"]


def ends(sink):
    return [c for k, c in sink.events if k == "end"]


def one(sink, op):
    matches = [c for c in starts(sink) if c.op_name == op]
    assert len(matches) == 1, f"expected exactly one {op}, got {len(matches)}"
    return matches[0]


def end_of(sink, call_id):
    matches = [c for c in ends(sink) if c.id == call_id]
    return matches[-1] if matches else None
