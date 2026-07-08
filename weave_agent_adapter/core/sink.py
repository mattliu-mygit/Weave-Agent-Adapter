"""Sink interface (spec 06 boundary).

A Sink receives the tracer's `WeaveCall` start/end emissions. Concrete sinks
live in the `sinks/` package: `WeaveSink` (logs to Weave), `RecordingSink`
(in-memory, tests), `DebugSink` (writes the tree to a file).
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
