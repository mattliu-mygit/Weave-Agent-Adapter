"""In-memory sink for tests: keeps ordered start/end emissions."""
from __future__ import annotations

from ..core.model import WeaveCall
from ..core.sink import Sink


class RecordingSink(Sink):
    def __init__(self) -> None:
        self.events: list[tuple[str, WeaveCall]] = []

    def start(self, call: WeaveCall) -> None:
        self.events.append(("start", call))

    def end(self, call: WeaveCall) -> None:
        self.events.append(("end", call))
