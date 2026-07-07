"""Trace sinks (spec 06 boundary).

A Sink receives `WeaveCall` start/end emissions from the tracer. The real
Weave-backed sink (SDK ingestion) lands with the sidecar; `RecordingSink`
collects calls in memory for tests and dry runs.
"""
from __future__ import annotations

from .model import WeaveCall


class Sink:
    def start(self, call: WeaveCall) -> None:  # noqa: D401
        raise NotImplementedError

    def end(self, call: WeaveCall) -> None:
        raise NotImplementedError


class RecordingSink(Sink):
    """In-memory sink for tests: keeps ordered start/end emissions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, WeaveCall]] = []

    def start(self, call: WeaveCall) -> None:
        self.events.append(("start", call))

    def end(self, call: WeaveCall) -> None:
        self.events.append(("end", call))
