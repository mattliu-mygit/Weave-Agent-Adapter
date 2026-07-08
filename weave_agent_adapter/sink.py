"""Trace sinks (spec 06 boundary).

A Sink receives `WeaveCall` start/end emissions from the tracer. `WeaveSink`
(in weave_sink.py) logs to Weave; `RecordingSink` collects in memory for tests;
`DebugSink` writes the tree to a file for local inspection without Weave.
"""
from __future__ import annotations

from .model import WeaveCall


class Sink:
    def start(self, call: WeaveCall) -> None:
        raise NotImplementedError

    def end(self, call: WeaveCall) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass


class RecordingSink(Sink):
    """In-memory sink for tests: keeps ordered start/end emissions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, WeaveCall]] = []

    def start(self, call: WeaveCall) -> None:
        self.events.append(("start", call))

    def end(self, call: WeaveCall) -> None:
        self.events.append(("end", call))


class DebugSink(RecordingSink):
    """Writes the finished trace tree to a file on flush — local inspection, no Weave."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def flush(self) -> None:
        starts = {c.id: c for k, c in self.events if k == "start"}
        ended = {c.id for k, c in self.events if k == "end"}
        end_attrs = {c.id: (c.attributes or {}) for k, c in self.events if k == "end"}
        children: dict = {}
        for c in starts.values():
            children.setdefault(c.parent_id, []).append(c)
        lines: list[str] = []

        def walk(cid: str, depth: int) -> None:
            c = starts[cid]
            a = end_attrs.get(cid, {}).get("weave_agent_adapter", {})
            tag = a.get("status") or a.get("decision") or a.get("steering_kind") or ""
            mark = "" if cid in ended else " (open)"
            lines.append("  " * depth + c.op_name + (f"  · {tag}" if tag else "") + mark)
            for ch in children.get(cid, []):
                walk(ch.id, depth + 1)

        for root in children.get(None, []):
            walk(root.id, 0)
        with open(self.path, "w") as f:
            f.write("\n".join(lines) + "\n")
