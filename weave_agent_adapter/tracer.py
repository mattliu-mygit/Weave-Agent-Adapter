"""Event → span reducer (specs 01, 05, 06).

Turns normalized hook events into a nested tree of `WeaveCall` start/end
emissions on a `Sink`. Holds per-session state in memory; all harness-specific
knowledge stays in the `Profile`. Canonical actions handled: session, turn,
tool (pre/post/error), permission (request/denied), subagent (start/stop),
compaction. A harness maps only the events it emits, missing ones degrade
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
import os
import re
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
                 redactor: Redactor = None, session_rate: float = 1.0,
                 project_per_repo: bool = False) -> None:
        self.profile = profile
        self.project = project
        self.sink = sink
        self.redactor = redactor or Redactor()
        self.session_rate = session_rate
        self.project_per_repo = project_per_repo
        self.sessions: dict[str, Session] = {}

    def _project_for(self, cwd) -> str:
        # per-repo: the working directory's leaf name (the repo folder), sanitized
        # to Weave's allowed charset; falls back to the configured default project
        if not self.project_per_repo or not cwd:
            return self.project
        leaf = os.path.basename(os.path.normpath(cwd))
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", leaf).strip("-")
        return slug or self.project

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
            return  # unmapped native event, ignore
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
        cwd = f.get("cwd")
        s = Session(
            session_id=sid, trace_id=_id(), root_call_id=_id(), project=self._project_for(cwd),
            started_at=at, last_activity=at,
            permission_mode=f.get("permission_mode"), cwd=cwd,
        )
        self.sessions[sid] = s
        self.sink.start(WeaveCall(
            id=s.root_call_id, trace_id=s.trace_id, op_name=f"{NS}.session",
            started_at=at, parent_id=None, inputs={"session_id": sid}, project=s.project,
            attributes={NS: {"kind": "session", "harness": self.profile.name,
                             "permission_mode": s.permission_mode, "cwd": s.cwd}},
        ))

    def _on_session_end(self, sid, f, at) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            self._finalize(s, at)

    def _finalize(self, s: Session, at, incomplete: bool = False) -> None:
        self._close_turn(s, at)
        s.status = SessionStatus.CLOSED
        out = {"turn_count": s.turn_count, "status": s.status.value}
        if incomplete:
            out["incomplete"] = True          # swept: no session_end ever arrived
        self.sink.end(WeaveCall(
            id=s.root_call_id, trace_id=s.trace_id, op_name=f"{NS}.session",
            started_at=s.started_at, ended_at=at, output=out, project=s.project,
        ))

    def sweep(self, now: float, ttl: float) -> int:
        """Finalize sessions idle past `ttl` (a harness that crashed before
        session_end), so state can't grow without bound and the trace closes
        rather than dangling. Returns how many were swept."""
        stale = [sid for sid, s in self.sessions.items() if now - s.last_activity > ttl]
        for sid in stale:
            s = self.sessions.pop(sid)
            try:
                self._finalize(s, s.last_activity, incomplete=True)
            except Exception:
                pass
        return len(stale)

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
            parent_id=s.root_call_id, inputs={"prompt": t.input_text},
            attributes={NS: {"kind": "turn", "index": t.index}},
        ))
        self._instant(s, t.call_id, f"{NS}.input", at,
                      inputs={"prompt": t.input_text}, attrs={"kind": "input"})

    def _on_turn_end(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s:
            return
        if s.current_turn and s.current_turn.open:
            s.current_turn.output_text = self.redactor.scrub(f.get("assistant_message"))
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
            output={"assistant": t.output_text, "status": t.status.value,
                    "tool_count": len(t.tool_order), "had_steering": bool(t.steering)},
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
            started_at=at, parent_id=self._tool_parent(s, t, f, at),
            inputs={"tool_name": tc.tool_name, "tool_input": tc.tool_input},
            attributes={NS: attrs},
        ))
        return tc

    def _tool_parent(self, s, t, f, at) -> str:
        # a tool run inside a subagent carries that subagent's agent_id -> nest it
        # under the subagent span (lazily opened if the harness has no start event)
        aid = f.get("agent_id")
        if not aid:
            return t.call_id
        rec = t.subagents.get(aid)
        if rec is None:
            rec = self._open_subagent(s, t, aid, f.get("agent_type") or "agent", at)
        return rec["call_id"]

    def _open_subagent(self, s, t, aid, atype, at, task=None, spawn=None) -> dict:
        cid = _id()
        rec = {"call_id": cid, "started_at": at, "type": atype}
        t.subagents[aid] = rec
        inputs = {"agent_type": atype}
        if task is not None:
            inputs["task"] = self.redactor.scrub(task)
        self.sink.start(WeaveCall(
            id=cid, trace_id=s.trace_id, op_name=f"{NS}.agent.{atype}",
            started_at=at, parent_id=t.call_id, inputs=inputs,
            attributes={NS: {"kind": "subagent", "agent_type": atype, "agent_id": aid,
                             "spawning_tool_use_id": spawn}},
        ))
        return rec

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
        self._open_subagent(s, s.current_turn, f.get("agent_id") or _id(),
                            f.get("agent_type") or "agent", at,
                            task=f.get("agent_task"), spawn=f.get("tool_use_id"))

    def _on_subagent_stop(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return
        t = s.current_turn
        aid = f.get("agent_id")
        rec = t.subagents.pop(aid, None) if aid else None
        if rec is None and t.subagents:
            aid, rec = t.subagents.popitem()      # no id match: close most-recent (LIFO)
        atype = (rec["type"] if rec else f.get("agent_type")) or "agent"
        aid = aid or f.get("agent_id")
        started = rec["started_at"] if rec else at   # stop-only: zero-duration marker
        cid = rec["call_id"] if rec else _id()
        if rec is None:
            # stop-only harness (e.g. Claude Code has no SubagentStart): the span
            # is just the completion, but still carries the subagent's identity + reply
            self.sink.start(WeaveCall(
                id=cid, trace_id=s.trace_id, op_name=f"{NS}.agent.{atype}",
                started_at=started, parent_id=t.call_id,
                inputs={"agent_type": atype, "agent_id": aid},
                attributes={NS: {"kind": "subagent", "phase": "stop", "agent_type": atype}},
            ))
        self.sink.end(WeaveCall(
            id=cid, trace_id=s.trace_id, op_name=f"{NS}.agent.{atype}",
            started_at=started, ended_at=at,
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
