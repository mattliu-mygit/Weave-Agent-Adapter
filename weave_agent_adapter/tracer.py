"""Reduce profile-normalized hook events into finalized agent turns.

Reduces normalized hook events into the in-memory domain model (Session/Turn/
ToolCall) and hands each *finalized* turn to the configured emitter once. A
turn finalizes when the next turn starts or the session ends/sweeps — not at
turn_end — because subagents can keep reporting work after the harness's Stop.

All harness knowledge lives in the `Profile` (event/field mapping, subagent
launcher tools, thread-id derivation). Canonical actions: session, turn, tool
(pre/post/error), permission (request/denied), subagent (start/stop),
compaction. A harness maps only the events it emits; missing ones degrade
gracefully (no session_end -> sweep closes; no tool_pre -> span synthesized
from the completion; no subagent_start -> record created at first sight).

Timing comes from the events' `captured_at`. Tool calls
key off `tool_use_id` when present; ID-less events require one unambiguous
running match by tool name and compatible input.
"""
from __future__ import annotations

import hashlib
import json

from .config import resolve_project
from .config_surface import config_version
from .model import (
    Decision, Permission, Session, Steering, SteeringKind, ToolCall, ToolStatus, Turn,
)
from .enrich import make_enricher
from .profile import Profile
from .redact import Redactor


class Tracer:
    def __init__(self, profile: Profile, project: str, emitter=None,
                 redactor: Redactor = None, session_rate: float = 1.0,
                 project_per_repo: bool = False) -> None:
        self.profile = profile
        self.project = project
        self.emitter = emitter
        self.redactor = redactor or Redactor()
        self.session_rate = session_rate
        self.project_per_repo = project_per_repo
        self.enricher = make_enricher(profile.enrich, self.redactor)
        self.sessions: dict[str, Session] = {}

    def _project_for(self, cwd) -> str:
        return resolve_project(self.project, cwd, self.project_per_repo)

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
        # Resume/edit continue under a NEW session_id that never gets its own
        # SessionStart; a sidecar restart also loses a live session. Auto-create
        # the session from the first event we see for an unknown sid, so its
        # turns/tools/subagents aren't silently dropped.
        if canonical != "session_start" and sid not in self.sessions:
            self._on_session_start(sid, fields, wire.captured_at)
            if canonical != "turn_start":
                s = self.sessions.get(sid)
                if s and s.current_turn is None:
                    s.current_turn = Turn(started_at=wire.captured_at, input_text="(resumed)")
        handler = getattr(self, f"_on_{canonical}", None)
        if handler:
            handler(sid, fields, wire.captured_at)
        s = self.sessions.get(sid)
        if s:
            if s.current_turn:
                for name in ("model", "permission_mode", "turn_id"):
                    if fields.get(name) is not None:
                        setattr(s.current_turn, name, fields[name])
            s.last_activity = wire.captured_at

    # ------- session -------

    def _on_session_start(self, sid, f, at) -> None:
        if sid in self.sessions or not self._sampled(sid):
            return
        cwd = f.get("cwd")
        s = Session(
            session_id=sid, project=self._project_for(cwd), last_activity=at,
            harness=self.profile.name, transcript=f.get("transcript"), cwd=cwd,
        )
        if self.profile.thread.get("source") == "field":
            s.thread_id = f.get(self.profile.thread.get("id_field"))
        paths = self.profile.config_surface.get("paths")
        if paths:
            try:
                s.config_version = config_version(paths, cwd=cwd)
            except Exception:
                pass                          # fingerprinting must never break tracing
        self.sessions[sid] = s

    def _on_session_end(self, sid, f, at) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            self._finalize(s, at)

    def _finalize(self, s: Session, at, incomplete: bool = False) -> None:
        t = s.current_turn
        if t and t.ended_at is None:
            t.incomplete = incomplete
            self._close_turn(s, at)
        self._emit_pending_turn(s)

    def sweep(self, now: float, ttl: float) -> int:
        """Finalize sessions idle past `ttl` (a harness that crashed before
        session_end), so state can't grow without bound and the pending turn
        still reaches the emitter. Returns how many were swept."""
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
        if s.current_turn and s.current_turn.ended_at is None:
            # a user message mid-turn is steering, not a new turn
            s.current_turn.steering.append(
                Steering(kind=SteeringKind.INTERJECTION, at=at, text=prompt))
            return
        self._emit_pending_turn(s)            # previous turn is final once the next begins
        s.current_turn = Turn(started_at=at, input_text=prompt,
                              effort_level=f.get("effort_level"))

    def _on_turn_end(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s:
            return
        if s.current_turn and s.current_turn.ended_at is None:
            s.current_turn.output_text = self.redactor.scrub(f.get("assistant_message"))
        self._close_turn(s, at)

    def _close_turn(self, s: Session, at) -> None:
        t = s.current_turn
        if not t or t.ended_at is not None:
            return
        t.ended_at = at
        # NOT emitted yet: subagents can finish after the harness's Stop, so the
        # turn stays pending until the next turn starts or the session finalizes.

    def _emit_pending_turn(self, s: Session) -> bool:
        t = s.current_turn
        if not t or t.ended_at is None:
            return False
        self._thread_of(s)                    # resolve the conversation id once
        if self.enricher:
            try:
                self.enricher.enrich_turn(t, s)   # LLM-call internals from the transcript
            except Exception:
                pass
        if self.emitter:
            try:
                self.emitter.emit_turn(t, s)
            except Exception:
                pass                          # an emitter must never break the reducer
        s.current_turn = None
        return True

    def finalize_idle_turns(self, now: float, linger: float) -> int:
        """Emit closed-but-pending turns whose session has been quiet for
        `linger` seconds — so a conversation's LAST turn appears promptly
        instead of waiting for session end or the sweep. Async subagent work
        resets last_activity, so lingering turns still absorb it."""
        n = 0
        for s in self.sessions.values():
            t = s.current_turn
            if (t and t.ended_at is not None
                    and not self._turn_has_open_children(t)
                    and now - s.last_activity > linger):
                self._emit_pending_turn(s)
                n += 1
        return n

    @staticmethod
    def _turn_has_open_children(t: Turn) -> bool:
        return (any(tc.status == ToolStatus.RUNNING for tc in t.tool_calls.values())
                or any(rec.get("ended_at") is None for rec in t.subagents.values()))

    def has_active_work(self) -> bool:
        for s in self.sessions.values():
            t = s.current_turn
            if t:
                return True
        return False

    def _thread_of(self, s: Session):
        # The conversation id links forks/resumes. How to get it is per-harness,
        # declared in the profile's [thread] section:
        #   source = "field"            -> a [fields] value carries it (resolved at
        #                                  session start); nothing to do here.
        #   source = "transcript_root"  -> the id of the conversation's first message,
        #                                  copied verbatim into every fork. Read the
        #                                  transcript once: first row not skipped and
        #                                  carrying id_key. Streamed + capped.
        #   (absent / other)            -> no thread linking.
        if s.thread_id is not None:
            return s.thread_id
        cfg = self.profile.thread
        if cfg.get("source") == "transcript_root" and s.transcript:
            skip_field = cfg.get("skip_field", "isSidechain")
            id_key = cfg.get("id_key", "uuid")
            try:
                with open(s.transcript) as fh:
                    for i, line in enumerate(fh):
                        if i >= 50:           # bound the scan; the first message is at the top
                            break
                        line = line.strip()
                        if not line:
                            continue
                        r = json.loads(line)
                        if not r.get(skip_field) and r.get(id_key):
                            s.thread_id = r.get(id_key)
                            break
            except Exception:
                pass
        return s.thread_id

    # ------- tools -------

    def _on_tool_pre(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if s and s.current_turn:
            self._open_tool(s.current_turn, f, at)

    def _open_tool(self, t: Turn, f, at) -> ToolCall:
        key = f.get("tool_use_id") or f"_synth:{f.get('tool_name')}:{len(t.tool_calls)}"
        tc = ToolCall(correlation_key=key, tool_name=f.get("tool_name", "tool"),
                      tool_input=self.redactor.scrub(f.get("tool_input") or {}),
                      started_at=at, agent_id=f.get("agent_id"))
        t.tool_calls[key] = tc
        if tc.agent_id and tc.agent_id not in t.subagents:
            # Some harnesses omit subagent_start. Materialize the record from
            # the first interior tool so the canonical model stays complete.
            self._open_subagent(t, tc.agent_id, f.get("agent_type") or "agent", at)
        return tc

    def _on_permission_request(self, sid, f, at) -> None:
        # recorded on the tool (prompt shown), not an event of its own
        _, tc = self._locate_tool(sid, f)
        if tc:
            tc.permission = Permission()

    def _on_permission_denied(self, sid, f, at) -> None:
        _, tc = self._locate_tool(sid, f)
        if not tc:
            return
        p = tc.permission or Permission()
        p.decision, p.reason = Decision.DENY, f.get("denial_reason")
        tc.permission = p
        tc.status = ToolStatus.REJECTED
        tc.ended_at = at

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
            # pre-tool event: reconstruct the tool from the completion alone
            tc = self._open_tool(s.current_turn, f, at)
        elif tc.status != ToolStatus.RUNNING:
            return
        # approval is inferred: a tool that ran was allowed
        if tc.permission:
            tc.permission.decision = Decision.ALLOW
        tc.status = ToolStatus.OK if ok else ToolStatus.ERROR
        tc.ended_at = at
        tc.output = self.redactor.scrub(f.get("tool_output")) if ok else None
        tc.error = None if ok else self.redactor.scrub(f.get("tool_output") or "error")

    # ------- subagents & compaction -------

    def _open_subagent(self, t: Turn, aid, atype, at, output=None, closed=False) -> dict:
        rec = {"agent_id": aid, "type": atype, "started_at": at,
               "ended_at": at if closed else None, "output": output}
        t.subagents[aid] = rec
        return rec

    def _on_subagent_start(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return
        aid = f.get("agent_id")
        if aid and aid not in s.current_turn.subagents:
            self._open_subagent(s.current_turn, aid, f.get("agent_type") or "agent", at)

    def _on_subagent_stop(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return
        t = s.current_turn
        aid = f.get("agent_id")
        # match strictly by agent_id; no LIFO fallback (a background SubagentStop
        # with a different agent_id would close a real subagent early)
        rec = t.subagents.get(aid) if aid else None
        if rec is not None and rec["ended_at"] is not None:
            return                            # already closed; a repeat stop is noise
        if rec is None:
            if not aid or not f.get("agent_type"):
                return
            self._open_subagent(t, aid, f.get("agent_type"), at, closed=True)
            return
        rec["ended_at"] = at
        rec["output"] = self.redactor.scrub(f.get("agent_output"))

    def _on_compaction(self, sid, f, at) -> None:
        s = self.sessions.get(sid)
        if s and s.current_turn:
            s.current_turn.compactions.append((at, f.get("compaction_trigger")))

    # ------- helpers -------

    def _locate_tool(self, sid, f):
        s = self.sessions.get(sid)
        if not s or not s.current_turn:
            return s, None
        t = s.current_turn
        key = f.get("tool_use_id")
        if key:
            return s, t.tool_calls.get(key)
        candidates = [tool for tool in t.tool_calls.values()
                      if tool.status == ToolStatus.RUNNING]
        name = f.get("tool_name")
        if name:
            candidates = [tc for tc in candidates if tc.tool_name == name]
        supplied_input = f.get("tool_input")
        if supplied_input:
            scrubbed = self.redactor.scrub(supplied_input)
            candidates = [tc for tc in candidates if tc.tool_input == scrubbed]
        if len(candidates) == 1:
            return s, candidates[0]
        return s, None
