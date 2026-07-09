"""Fan a call stream out to several sinks; one sink's failure never hits another."""
from __future__ import annotations

from ..core.model import WeaveCall
from ..core.sink import Sink


class TeeSink(Sink):
    def __init__(self, sinks: list):
        self.sinks = sinks

    def _each(self, method: str, *args) -> None:
        for s in self.sinks:
            try:
                getattr(s, method)(*args)
            except Exception:
                pass

    def start(self, wc: WeaveCall) -> None:
        self._each("start", wc)

    def end(self, wc: WeaveCall) -> None:
        self._each("end", wc)

    def flush(self) -> None:
        self._each("flush")
