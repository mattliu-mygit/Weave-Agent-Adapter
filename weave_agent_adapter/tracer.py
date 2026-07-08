"""Event → span reducer (specs 01, 05, 06).

Turns normalized hook events into a nested tree of `WeaveCall` start/end
emissions on a `Sink`. Holds per-session state in memory; all harness-specific
knowledge stays in the `Profile`. Canonical actions handled: session, turn,
tool (pre/post/error), permission (request/denied), subagent (start/stop),
compaction. A harness maps only the events it emits — missing ones degrade
gracefully (e.g. stop-only subagents annotate rather than span).

Timing: every emission's time comes from the event's `captured_at`.
Correlation (spec 05): tool calls key off the profile's `tool_use_id` field
when present, else fall back to the last still-running tool (LIFO). The
transcript-based fallback is deferred until M0 confirms the payload schema.

Provisional field names (confirmed/adjusted via M0, and only in the profile):
`prompt`, `tool_output`, `tool_use_id`, `denial_reason`.
"""
from __future__ import annotations

import hashlib
import uuid

from .core.model import (
    Decision, Permission, Session, SessionStatus, Steering,
    SteeringKind, ToolCall, ToolStatus, Turn, TurnStatus, WeaveCall,
)
from .core.sink import Sink
from .profile import Profile
from .redact import Redactor

NS = "weave_agent_adapter"


def _id() -> str:
    return str(uuid.uuid4())          # Weave requires round-trippable UUID ids


class Tracer:
    def __init__(self, profile: Profile, project: str, sink: Sink,
                 redactor: Redactor = None, session_rate: float = 1.0) -> None:
        self.profile = profile
        self.project = project
        self.sink = sink
        self.redactor = redactor or Redactor()
        self.session_rate = session_rate
        self.sessions: dict[str, Session] = {}

    def _sampled(self, sid: str) -> bool:
        # deterministic per session_id, so a session is all-in or all-out
        if self.session_rate >= 1.0:
            return True
        if self.session_rate <= 0.0:
            return False
        h = int.from_bytes(hashlib.md5(sid.encode()).digest()[:4], "big") / 2 ** 32
        return h < self.session_rate

    def handle(self, wire) -> None:
        canonical = self.profile.canonical_event(wire.event)
        if canonical is None:
            return  # unmapped native event — ignore
        fields = self.profile.extract(wire.payload)
        sid = fields.get("session_id")
        if not sid:
            return
        handler = getattr(self, f"_on_{canonical}", None)
        if handler:
            handler(sid, fields, wire.captured_at)
        s = self.sessions.get(sid)
        if s:
            s.last_activity = wire.captured_at

    # ------- session -------

    def _on_session_start(self, sid, f, at) -> None:
        if sid in self.sessions or not self._sampled(sid):
            return
        s = Session(
            session_id=sid, trace_id=_id(), root_call_id=_id(), project=self.project,
            started_at=at, last_activity=at,
            permission_mode=f.get("permission_mode"), cwd=f.get("cwd"),
        )
        self.sessions[sid] = s
        self.sink.start(WeaveCall(
            id=s.root_call_id, trace_id=s.trace_id, op_name=f"{NS}.session",
            started_at=at, parent_id=None, inputs={"session_id": sid},
            attributes={NS: {"kind": "session", "harness": self.profile.name,
                             "permission_mode": s.permission_mode, "cwd": s.cwd}},
        ))

    def _on_session_end(self, sid, f, at) -> None:
        s = self.sessions.pop(sid, None)
        if not s:
            return
        self._close_turn(s, at)
        s.status = SessionStatus.CLOSED
        self.sink.end(WeaveCall(
            id=s.root_call_id, trace_id=s.trace_id, op_name=f"{NS}.session",
            started_at=s.started_at, ended_at=at,
            output={"turn_count": s.turn_count, "status": s.status.value},
        ))

    # ------- turn -------

    def _on_turn_start(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s:
            return
        prompt = self.redactor.scrub(f.get("prompt"), "prompt")
        if s.current_turn and s.current_turn.open:
            # a user message mid-turn is steering, not a new turn
            self._emit_steering(s, at, SteeringKind.INTERJECTION, text=prompt)
            return
        t = Turn(call_id=_id(), index=s.turn_count, started_at=at, input_text=prompt)
        s.current_turn = t
        s.turn_count += 1
        self.sink.start(WeaveCall(
            id=t.call_id, trace_id=s.trace_id, op_name=f"{NS}.turn", started_at=at,
            parent_id=s.root_call_id, attributes={NS: {"kind": "turn", "index": t.index}},
        ))
        self._instant(s, t.call_id, f"{NS}.input", at,
                      inputs={"prompt": t.input_text}, attrs={"kind": "input"})

    def _on_turn_end(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if s:
            self._close_turn(s, at)

    def _close_turn(self, s: Session, at) -> None:
        t = s.current_turn
        if not t or not t.open:
            return
        self._instant(s, t.call_id, f"{NS}.stop", at, attrs={"kind": "stop"})
        t.open = False
        t.ended_at = at
        t.status = TurnStatus.CLOSED
        self.sink.end(WeaveCall(
            id=t.call_id, trace_id=s.trace_id, op_name=f"{NS}.turn",
            started_at=t.started_at, ended_at=at,
            output={"status": t.status.value, "tool_count": len(t.tool_order),
                    "had_steering": bool(t.steering)},
        ))

    # ------- tools -------

    def _on_tool_pre(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if s and s.current_turn:
            self._open_tool(s, f, at)

    def _open_tool(self, s, f, at, partial=False) -> ToolCall:
        t = s.current_turn
        key = f.get("tool_use_id") or f"_synth:{f.get('tool_name')}:{len(t.tool_order)}"
        tc = ToolCall(correlation_key=key, call_id=_id(),
                      tool_name=f.get("tool_name", "tool"),
                      tool_input=self.redactor.scrub(f.get("tool_input") or {}), started_at=at)
        t.tool_calls[key] = tc
        t.tool_order.append(key)
        attrs = {"kind": "tool", "tool_name": tc.tool_name, "harness": self.profile.name}
        if partial:
            attrs["partial"] = True          # harness has no pre-tool hook
        self.sink.start(WeaveCall(
            id=tc.call_id, trace_id=s.trace_id, op_name=f"{NS}.tool.{tc.tool_name}",
            started_at=at, parent_id=t.call_id,
            inputs={"tool_name": tc.tool_name, "tool_input": tc.tool_input},
            attributes={NS: attrs},
        ))
        return tc

    def _on_permission_request(self, sid, f, at) -> None:
        # recorded on the tool (prompt shown), not a span of its own
        s, tc = self._locate_tool(sid, f)
        if tc:
            tc.permission = Permission(requested_at=at)

    def _on_permission_denied(self, sid, f, at) -> None:
        s, tc = self._locate_tool(sid, f)
        if not tc:
            return
        p = tc.permission or Permission()
        p.decision, p.reason = Decision.DENY, f.get("denial_reason")
        tc.permission = p
        tc.status = ToolStatus.REJECTED
        tc.ended_at = at
        self.sink.end(WeaveCall(
            id=tc.call_id, trace_id=s.trace_id, op_name=f"{NS}.tool.{tc.tool_name}",
            started_at=tc.started_at, ended_at=at, output=None,
            attributes={NS: {"status": "rejected", "permission_decision": "deny",
                             "denial_reason": p.reason,
                             "prompt_shown": p.requested_at is not None}},
        ))

    def _on_tool_post(self, sid, f, at) -> None:
        self._finish_tool(sid, f, at, ok=True)

    def _on_tool_error(self, sid, f, at) -> None:
        self._finish_tool(sid, f, at, ok=False)

    def _finish_tool(self, sid, f, at, ok: bool) -> None:
        s, tc = self._locate_tool(sid, f)
        if not s or not s.current_turn:
            return
        if tc is None:
            # fallback for a bring-your-own harness whose hook system has no
            # pre-tool event: reconstruct the span from the completion alone
            tc = self._open_tool(s, f, at, partial=True)
        elif tc.status != ToolStatus.RUNNING:
            return
        # approval is inferred: a tool that ran was allowed
        prompted = bool(tc.permission and tc.permission.requested_at)
        if tc.permission:
            tc.permission.decision = Decision.ALLOW
        tc.status = ToolStatus.OK if ok else ToolStatus.ERROR
        tc.ended_at = at
        tc.output = self.redactor.scrub(f.get("tool_output")) if ok else None
        tc.error = None if ok else self.redactor.scrub(f.get("tool_output") or "error")
        self.sink.end(WeaveCall(
            id=tc.call_id, trace_id=s.trace_id, op_name=f"{NS}.tool.{tc.tool_name}",
            started_at=tc.started_at, ended_at=at, output=tc.output, exception=tc.error,
            attributes={NS: {"status": tc.status.value, "permission_decision": "allow",
                             "permission_source": "user" if prompted else "auto",
                             "prompt_shown": prompted}},
        ))

    # ------- subagents & compaction -------

    def _on_subagent_start(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return
        t = s.current_turn
        aid = f.get("agent_id") or _id()
        atype = f.get("agent_type") or "agent"
        cid = _id()
        t.subagents[aid] = {"call_id": cid, "started_at": at, "type": atype}
        self.sink.start(WeaveCall(
            id=cid, trace_id=s.trace_id, op_name=f"{NS}.agent.{atype}",
            started_at=at, parent_id=t.call_id,
            inputs={"agent_type": atype, "task": self.redactor.scrub(f.get("agent_task"))},
            attributes={NS: {"kind": "subagent", "agent_type": atype, "agent_id": aid,
                             "spawning_tool_use_id": f.get("tool_use_id")}},
        ))

    def _on_subagent_stop(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return
        t = s.current_turn
        aid = f.get("agent_id")
        rec = t.subagents.pop(aid, None) if aid else None
        if rec is None and t.subagents:
            aid, rec = t.subagents.popitem()      # no id match: close most-recent (LIFO)
        if rec is None:
            # stop-only harness (e.g. Claude Code has no SubagentStart): annotate completion
            self._instant(s, t.call_id, f"{NS}.agent.{f.get('agent_type') or 'agent'}", at,
                          attrs={"kind": "subagent", "phase": "stop"})
            return
        self.sink.end(WeaveCall(
            id=rec["call_id"], trace_id=s.trace_id, op_name=f"{NS}.agent.{rec['type']}",
            started_at=rec["started_at"], ended_at=at,
            output=self.redactor.scrub(f.get("agent_output")),
            attributes={NS: {"status": "ok"}},
        ))

    def _on_compaction(self, sid, f, at) -> None:
        # context compaction is session-level: annotate under the root
        s = self.sessions.get(sid)
        if not s:
            return
        self._instant(s, s.root_call_id, f"{NS}.compaction", at,
                      attrs={"kind": "compaction", "trigger": f.get("compaction_trigger")})

    # ------- helpers -------

    def _locate_tool(self, sid, f):
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return s, None
        t = s.current_turn
        key = f.get("tool_use_id")
        if key and key in t.tool_calls:
            return s, t.tool_calls[key]
        for k in reversed(t.tool_order):          # fallback: last still-running tool
            if t.tool_calls[k].status == ToolStatus.RUNNING:
                return s, t.tool_calls[k]
        return s, None

    def _emit_steering(self, s, at, kind, text=None) -> None:
        t = s.current_turn
        if not t:
            return
        t.steering.append(Steering(kind=kind, at=at, text=text))
        self._instant(s, t.call_id, f"{NS}.steering", at,
                      inputs={"text": text} if text else {},
                      attrs={"kind": "steering", "steering_kind": kind.value})

    def _instant(self, s, parent_id, op_name, at, inputs=None, attrs=None) -> None:
        cid = _id()
        self.sink.start(WeaveCall(id=cid, trace_id=s.trace_id, op_name=op_name, started_at=at,
                                  parent_id=parent_id, inputs=inputs or {},
                                  attributes={NS: attrs or {}}))
        self.sink.end(WeaveCall(id=cid, trace_id=s.trace_id, op_name=op_name,
                                started_at=at, ended_at=at))
