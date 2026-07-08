"""Weave-backed sink (spec 06): log WeaveCalls to W&B Weave.

Uses the low-level `call_start` / `call_end` trace-server API rather than the
`create_call` decorator path, because that's the only surface that accepts
**explicit timestamps** (`started_at`/`ended_at`) and **explicit ids**
(`id`/`trace_id`/`parent_id`). Our spans are reconstructed out-of-process, so we
must supply our own ids and our hook-stamped `captured_at` — otherwise Weave
would stamp wall-clock at ingest time and the durations would be wrong.

Delivery still goes through the client's async batch processor (`client.server`),
so batching and retry are unchanged, and spans land in Weave's native call model
(not raw OTel JSON). `weave` is imported lazily.
"""
from __future__ import annotations

import datetime
import os

from ..core.model import WeaveCall
from ..core.sink import Sink


def _dt(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


class WeaveSink(Sink):
    def __init__(self, project: str):
        # we trace via the low-level call API, not @weave.op, so integration
        # autopatching is pure init cost — turn it off.
        os.environ.setdefault("WEAVE_IMPLICITLY_PATCH_INTEGRATIONS", "false")
        import weave
        from weave.trace_server import trace_server_interface as tsi

        self._tsi = tsi
        self._client = weave.init(project)
        self._server = self._client.server
        self._project_id = f"{self._client.entity}/{self._client.project}"

    def start(self, wc: WeaveCall) -> None:
        self._server.call_start(self._tsi.CallStartReq(
            start=self._tsi.StartedCallSchemaForInsert(
                project_id=self._project_id, id=wc.id, op_name=wc.op_name,
                trace_id=wc.trace_id, parent_id=wc.parent_id,
                started_at=_dt(wc.started_at),
                attributes=wc.attributes or {}, inputs=wc.inputs or {},
            )
        ))

    def end(self, wc: WeaveCall) -> None:
        self._server.call_end(self._tsi.CallEndReq(
            end=self._tsi.EndedCallSchemaForInsert(
                project_id=self._project_id, id=wc.id,
                ended_at=_dt(wc.ended_at if wc.ended_at is not None else wc.started_at),
                output=wc.output, exception=wc.exception,
                summary=wc.attributes or {},
            )
        ))

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            pass
