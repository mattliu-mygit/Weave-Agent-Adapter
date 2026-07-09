"""GenAI-plane turn emitter: each finalized turn becomes one OTel GenAI trace.

Weave's Signals/agents surface listens to the OTel GenAI span plane (one trace
per *turn*, stitched into conversations by `gen_ai.conversation.id`). The
tracer hands this emitter the finished `Turn` object once it can no longer
change (next turn started / session finalized), so the span tree is rendered
straight from the domain model: `invoke_agent <harness>` root with prompt and
reply, `execute_tool` children, subagents as nested `invoke_agent` (their
interior tools inside), steering as span events. Timestamps are the
hook-captured ones.

`_build_turn` is pure and unit-testable; OTel is imported lazily and only when
a custom `emit` isn't injected. All failures are swallowed: the spans plane
must never break primary tracing.
"""
from __future__ import annotations

import json
import os

from .core.model import Session, ToolStatus, Turn

NS = "weave_agent_adapter"
DEFAULT_ENDPOINT = "https://trace.wandb.ai/agents/otel/v1/traces"
_UNSET = object()
_MAX_TOOL_OUTPUT = 32_000


def _api_key():
    key = os.environ.get("WANDB_API_KEY")
    if key:
        return key
    try:
        import netrc
        from urllib.parse import urlparse
        rc = netrc.netrc()
        hosts = ["api.wandb.ai"]
        base_url = os.environ.get("WANDB_BASE_URL")
        if base_url:
            hosts.insert(0, urlparse(base_url).hostname)
        for host in hosts:
            if host:
                auth = rc.authenticators(host)
                if auth and auth[2]:
                    return auth[2]
    except Exception:
        pass
    return None


class GenAITurnEmitter:
    def __init__(self, default_entity: str = None, endpoint: str = None, emit=None):
        self._default_entity = default_entity if default_entity is not None else _UNSET
        self._endpoint = endpoint or os.environ.get(
            "WEAVE_AGENT_ADAPTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
        self._emit = emit                         # injectable for tests
        self._providers: dict = {}                # project_id -> TracerProvider

    def emit_turn(self, turn: Turn, session: Session) -> None:
        node = self._build_turn(turn, session)
        (self._emit or self._emit_otel)(node, self._project_id(session))

    def flush(self) -> None:
        for p in self._providers.values():
            try:
                p.force_flush()
            except Exception:
                pass

    # ---- pure assembly (domain model -> span tree dict) ----

    def _build_turn(self, t: Turn, s: Session) -> dict:
        attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": s.harness or "agent",
            "gen_ai.conversation.id": str(s.thread_id or s.session_id),
            f"{NS}.session_id": str(s.session_id),
        }
        if t.input_text is not None:
            attrs["gen_ai.prompt.0.role"] = "user"
            attrs["gen_ai.prompt.0.content"] = str(t.input_text)
        if t.output_text is not None:
            attrs["gen_ai.completion.0.role"] = "assistant"
            attrs["gen_ai.completion.0.content"] = str(t.output_text)
        if t.incomplete:
            attrs[f"{NS}.incomplete"] = "true"   # closed by sweep, not by the harness

        children = [self._subagent_node(rec, t) for rec in t.subagents.values()]
        children += [self._tool_node(tc) for tc in self._tools_of(t, agent_id=None)]
        children += [self._chat_node(c) for c in t.chat_calls]
        children.sort(key=lambda n: n["start"])

        events = [{"name": "steering", "ts": st.at,
                   "attributes": {f"{NS}.steering.kind": st.kind.value,
                                  f"{NS}.steering.text": str(st.text)}}
                  for st in t.steering]
        events += [{"name": "compaction", "ts": at,
                    "attributes": {f"{NS}.compaction.trigger": str(trigger)}}
                   for at, trigger in t.compactions]

        return {
            "name": f"invoke_agent {s.harness or 'agent'}",
            "start": t.started_at,
            "end": t.ended_at if t.ended_at is not None else t.started_at,
            "attributes": attrs,
            "events": events,
            "children": children,
        }

    def _tools_of(self, t: Turn, agent_id) -> list:
        return [t.tool_calls[k] for k in t.tool_order
                if t.tool_calls[k].agent_id == agent_id]

    def _chat_node(self, c: dict) -> dict:
        # one span per LLM API call (from transcript enrichment); official token keys
        model = c.get("model") or "unknown"
        a = {"gen_ai.operation.name": "chat",
             "gen_ai.request.model": model, "gen_ai.response.model": model}
        for attr, key in (("gen_ai.usage.input_tokens", "input_tokens"),
                          ("gen_ai.usage.output_tokens", "output_tokens"),
                          ("gen_ai.usage.cache_read.input_tokens", "cache_read_tokens"),
                          ("gen_ai.usage.cache_creation.input_tokens", "cache_creation_tokens")):
            if c.get(key) is not None:
                a[attr] = int(c[key])
        if c.get("finish_reason"):
            a["gen_ai.response.finish_reasons"] = str(c["finish_reason"])
        if c.get("text"):
            a["gen_ai.completion.0.role"] = "assistant"
            a["gen_ai.completion.0.content"] = str(c["text"])
        return {"name": f"chat {model}", "start": c["started_at"], "end": c["ended_at"],
                "attributes": a, "children": []}

    def _tool_node(self, tc) -> dict:
        a = {"gen_ai.operation.name": "execute_tool",
             "gen_ai.tool.name": tc.tool_name,
             "gen_ai.tool.call.id": tc.correlation_key,
             "gen_ai.tool.call.arguments": json.dumps(tc.tool_input, default=str)}
        if tc.output is not None:
            result = json.dumps(tc.output, default=str)
            if len(result) > _MAX_TOOL_OUTPUT:
                result = result[:_MAX_TOOL_OUTPUT] + "…[truncated]"
            a["gen_ai.tool.call.result"] = result
        if tc.permission:
            a[f"{NS}.permission.decision"] = tc.permission.decision.value
            if tc.permission.reason:
                a[f"{NS}.permission.denial_reason"] = str(tc.permission.reason)
        a[f"{NS}.tool.status"] = tc.status.value
        return {
            "name": f"execute_tool {tc.tool_name}",
            "start": tc.started_at,
            "end": tc.ended_at if tc.ended_at is not None else tc.started_at,
            "attributes": a,
            "error": tc.status in (ToolStatus.ERROR, ToolStatus.REJECTED),
            "children": [],
        }

    def _subagent_node(self, rec: dict, t: Turn) -> dict:
        children = [self._tool_node(tc) for tc in self._tools_of(t, rec.get("agent_id"))]
        children.sort(key=lambda n: n["start"])
        attrs = {"gen_ai.operation.name": "invoke_agent",
                 "gen_ai.agent.name": rec["type"],
                 f"{NS}.agent_id": str(rec.get("agent_id"))}
        node = {
            "name": f"invoke_agent {rec['type']}",
            "start": rec["started_at"],
            "end": rec["ended_at"] if rec["ended_at"] is not None else rec["started_at"],
            "attributes": attrs,
            "children": children,
        }
        if rec.get("output") is not None:
            attrs["gen_ai.completion.0.role"] = "assistant"
            attrs["gen_ai.completion.0.content"] = str(rec["output"])
        return node

    # ---- OTel emission ----

    def _project_id(self, s: Session) -> str:
        p = s.project or "agent-sessions"
        if "/" in p:
            return p
        ent = self._entity()
        return f"{ent}/{p}" if ent else p

    def _entity(self) -> str:
        if self._default_entity is _UNSET:
            try:
                import wandb
                ent = wandb.Api().default_entity
                if ent:
                    self._default_entity = ent
            except Exception:
                pass
        return self._default_entity if self._default_entity is not _UNSET else ""

    def _emit_otel(self, node: dict, project_id: str) -> None:
        tracer = self._tracer(project_id)
        if tracer is None:
            return
        from opentelemetry.trace import Status, StatusCode, set_span_in_context

        def ns(ts: float) -> int:
            return int(ts * 1e9)

        def walk(n, ctx):
            span = tracer.start_span(n["name"], context=ctx, start_time=ns(n["start"]),
                                     attributes=n["attributes"])
            for ev in n.get("events", []):
                span.add_event(ev["name"], attributes=ev["attributes"], timestamp=ns(ev["ts"]))
            if n.get("error"):
                span.set_status(Status(StatusCode.ERROR))
            child_ctx = set_span_in_context(span)
            for ch in n.get("children", []):
                walk(ch, child_ctx)
            span.end(end_time=ns(n["end"]))

        walk(node, None)

    def _tracer(self, project_id: str):
        provider = self._providers.get(project_id)
        if provider is None:
            try:
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                entity, _, project = project_id.partition("/")
                key = _api_key()
                if not key:
                    return None
                provider = TracerProvider(resource=Resource.create(
                    {"wandb.entity": entity, "wandb.project": project}))
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                    endpoint=self._endpoint,
                    headers={"wandb-api-key": key, "project_id": project_id})))
                self._providers[project_id] = provider
            except Exception:
                return None
        return provider.get_tracer(NS)
