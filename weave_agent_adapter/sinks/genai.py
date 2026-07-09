"""GenAI-plane sink: dual-emit each closed turn as an OTel GenAI trace.

Weave's Signals/agents surface listens only to the OTel GenAI span plane (one
trace per *turn*, stitched into conversations by `gen_ai.conversation.id`) —
custom call ops never trigger it. This sink follows the official plugin's
precedent: buffer the adapter's call stream, and when a turn closes, emit its
subtree as `invoke_agent <harness>` with `execute_tool` / nested `invoke_agent`
children, timestamped from the hook-captured times.

Assembly (`_build_turn`) is pure and unit-testable; OTel is imported lazily and
only when a custom `emit` isn't injected. Every failure is swallowed: dual-emit
must never break primary tracing.
"""
from __future__ import annotations

import json
import os
import time

from ..core.model import WeaveCall
from ..core.sink import Sink

NS = "weave_agent_adapter"
DEFAULT_ENDPOINT = "https://trace.wandb.ai/agents/otel/v1/traces"
MAX_BUFFER = 10_000                      # safety cap on buffered calls


def _api_key():
    key = os.environ.get("WANDB_API_KEY")
    if key:
        return key
    try:
        import netrc
        auth = netrc.netrc().authenticators("api.wandb.ai")
        return auth[2] if auth else None
    except Exception:
        return None


class GenAISink(Sink):
    def __init__(self, project_id: str, endpoint: str = None, emit=None):
        self._project_id = project_id                     # "entity/project"
        self._endpoint = endpoint or os.environ.get(
            "WEAVE_AGENT_ADAPTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
        self._emit = emit                                 # injectable for tests
        self._calls: dict = {}                            # call_id -> {call, ended, children}
        self._provider = None

    # ---- call stream buffering ----

    def start(self, wc: WeaveCall) -> None:
        if len(self._calls) >= MAX_BUFFER:
            self._calls.clear()                           # degraded, never unbounded
        self._calls[wc.id] = {"call": wc, "end": None, "children": []}
        parent = self._calls.get(wc.parent_id)
        if parent:
            parent["children"].append(wc.id)

    def end(self, wc: WeaveCall) -> None:
        rec = self._calls.get(wc.id)
        if rec is None:
            return
        rec["end"] = wc
        op = rec["call"].op_name
        try:
            if op == f"{NS}.turn":
                turn = self._build_turn(wc.id)
                if turn:
                    (self._emit or self._emit_otel)(turn)
                self._drop_subtree(wc.id)
            elif op == f"{NS}.session":
                self._drop_subtree(wc.id)
        except Exception:
            pass

    def flush(self) -> None:
        if self._provider is not None:
            try:
                self._provider.force_flush()
            except Exception:
                pass

    # ---- pure turn assembly ----

    def _build_turn(self, turn_id: str):
        rec = self._calls[turn_id]
        call, endc = rec["call"], rec["end"]
        session = self._calls.get(call.parent_id)
        s_attrs = ((session or {}).get("call").attributes if session else {}) or {}
        harness = (s_attrs.get(NS) or {}).get("harness") or "agent"
        session_id = (((session or {}).get("call").inputs if session else {}) or {}).get("session_id")
        conv = call.thread_id or session_id or call.trace_id
        prompt = (call.inputs or {}).get("prompt")
        out = (endc.output or {}) if endc else {}
        assistant = out.get("assistant") if isinstance(out, dict) else None

        attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": harness,
            "gen_ai.conversation.id": str(conv),
        }
        if session_id:
            attrs[f"{NS}.session_id"] = str(session_id)
        if prompt is not None:
            attrs["gen_ai.prompt.0.role"] = "user"
            attrs["gen_ai.prompt.0.content"] = str(prompt)
        if assistant is not None:
            attrs["gen_ai.completion.0.role"] = "assistant"
            attrs["gen_ai.completion.0.content"] = str(assistant)

        return {
            "name": f"invoke_agent {harness}",
            "start": call.started_at,
            "end": (endc.ended_at if endc and endc.ended_at else call.started_at),
            "attributes": attrs,
            "children": self._child_spans(turn_id),
        }

    def _child_spans(self, parent_id: str) -> list:
        out = []
        for cid in self._calls[parent_id]["children"]:
            rec = self._calls.get(cid)
            if rec is None:
                continue
            call, endc = rec["call"], rec["end"]
            op = call.op_name
            ns_attrs = (call.attributes or {}).get(NS, {})
            end_attrs = ((endc.attributes or {}).get(NS, {}) if endc else {})
            node = None
            if f".tool." in op:
                tool = ns_attrs.get("tool_name") or op.rsplit(".", 1)[-1]
                a = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                     "gen_ai.tool.call.arguments": json.dumps(
                         (call.inputs or {}).get("tool_input"), default=str)}
                if endc and endc.output is not None:
                    a["gen_ai.tool.call.result"] = json.dumps(endc.output, default=str)
                for k in ("permission_decision", "permission_source", "prompt_shown", "denial_reason"):
                    if end_attrs.get(k) is not None:
                        a[f"weave.permission.{k}"] = str(end_attrs[k])
                node = {"name": f"execute_tool {tool}", "attributes": a}
            elif f".agent." in op:
                atype = ns_attrs.get("agent_type") or op.rsplit(".", 1)[-1]
                node = {"name": f"invoke_agent {atype}",
                        "attributes": {"gen_ai.operation.name": "invoke_agent",
                                       "gen_ai.agent.name": atype}}
            elif op.endswith(".steering"):
                node = {"name": "steering",
                        "attributes": {f"{NS}.steering.text": str((call.inputs or {}).get("text"))}}
            if node is None:
                continue                                   # input/stop markers: content is on the root
            node["start"] = call.started_at
            node["end"] = (endc.ended_at if endc and endc.ended_at else call.started_at)
            node["children"] = self._child_spans(cid)
            out.append(node)
        return out

    def _drop_subtree(self, root_id: str) -> None:
        rec = self._calls.pop(root_id, None)
        if not rec:
            return
        for cid in rec["children"]:
            self._drop_subtree(cid)

    # ---- OTel emission ----

    def _emit_otel(self, turn: dict) -> None:
        tracer = self._tracer()
        if tracer is None:
            return
        from opentelemetry.trace import set_span_in_context

        def ns(t: float) -> int:
            return int(t * 1e9)

        def walk(node, ctx):
            span = tracer.start_span(node["name"], context=ctx,
                                     start_time=ns(node["start"]),
                                     attributes=node["attributes"])
            child_ctx = set_span_in_context(span)
            for ch in node.get("children", []):
                walk(ch, child_ctx)
            span.end(end_time=ns(node["end"]))

        walk(turn, None)

    def _tracer(self):
        if self._provider is None:
            try:
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                entity, _, project = self._project_id.partition("/")
                key = _api_key()
                if not key:
                    return None
                provider = TracerProvider(resource=Resource.create(
                    {"wandb.entity": entity, "wandb.project": project}))
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                    endpoint=self._endpoint,
                    headers={"wandb-api-key": key, "project_id": self._project_id})))
                self._provider = provider
            except Exception:
                return None
        return self._provider.get_tracer(NS)
