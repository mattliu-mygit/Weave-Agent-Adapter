"""Concrete sinks. The `Sink` interface lives in `core.sink`."""
from ..core.sink import Sink
from .debug import DebugSink
from .recording import RecordingSink
from .weave import WeaveSink

__all__ = ["Sink", "RecordingSink", "DebugSink", "WeaveSink"]
