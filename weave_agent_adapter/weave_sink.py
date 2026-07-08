"""Weave-backed sink (spec 06): log WeaveCalls to W&B Weave.

Uses the SDK's low-level `create_call` / `finish_call` with an explicit parent
`Call` — our spans are built out-of-process, so there's no `@weave.op` / in-process
stack to nest through (`use_stack=False`). `weave.init()` runs once; Weave assigns
the ids and we map them from our span ids.

`weave` is imported lazily so importing this module (and the hook path) needs no
Weave install — only constructing a WeaveSink does.
"""
from __future__ import annotations

from .model import WeaveCall
from .sink import Sink


class WeaveSink(Sink):
    def __init__(self, project: str):
        import weave

        self._client = weave.init(project)
        self._calls: dict = {}          # our WeaveCall.id -> Weave Call

    def start(self, wc: WeaveCall) -> None:
        parent = self._calls.get(wc.parent_id)
        self._calls[wc.id] = self._client.create_call(
            op=wc.op_name, inputs=wc.inputs or {}, parent=parent,
            attributes=wc.attributes or {}, use_stack=False,
        )

    def end(self, wc: WeaveCall) -> None:
        call = self._calls.pop(wc.id, None)
        if call is None:
            return
        if wc.attributes:               # end-time metadata (status, decision, ...)
            call.summary = {**(call.summary or {}), **wc.attributes}
        exc = Exception(wc.exception) if wc.exception else None
        self._client.finish_call(call, output=wc.output, exception=exc)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            pass
