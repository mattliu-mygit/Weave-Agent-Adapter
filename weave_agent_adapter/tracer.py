"""Event → turn reducer (specs 01, 05).

Reduces normalized hook events into the in-memory domain model (Session/Turn/
ToolCall) and hands each *finalized* turn to the configured turn emitters. A
turn finalizes when the next turn starts or the session ends/sweeps — not at
turn_end — because subagents can keep reporting work after the harness's Stop.

All harness knowledge lives in the `Profile` (event/field mapping, subagent
launcher tools, thread-id derivation). Canonical actions: session, turn, tool
(pre/post/error), permission (request/denied), subagent (start/stop),
compaction. A harness maps only the events it emits; missing ones degrade
gracefully (no session_end -> sweep closes; no tool_pre -> span synthesized
from the completion; no subagent_start -> record created at first sight).

Timing comes from the events' `captured_at`. Correlation (spec 05): tool calls
key off `tool_use_id` when present, else the last still-running tool (LIFO).
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .core.model import (
    Decision, Permission, Session, Steering, SteeringKind, ToolCall, ToolStatus, Turn,
)
from .enrich import make_enricher
from .profile import Profile
from .redact import Redactor


class Tracer:
    def __init__(self, profile: Profile, project: str, turn_emitters: list = None,
                 redactor: Redactor = None, session_rate: float = 1.0,
                 project_per_repo: bool = False) -> None:
        self.profile = profile
        self.project = project
        self.turn_emitters = turn_emitters or []
        self.redactor = redactor or Redactor()
        self.session_rate = session_rate
        self.project_per_repo = project_per_repo
        self.enricher = make_enricher(profile.enrich, self.redactor)
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
        # Resume/edit continue under a NEW session_id that never gets its own
        # SessionStart; a sidecar restart also loses a live session. Auto-create
        # the session from the first event we see for an unknown sid, so its
        # turns/tools/subagents aren't silently dropped.
        if canonical != "session_start" and sid not in self.sessions:
            self._on_session_start(sid, fields, wire.captured_at)
            if canonical != "turn_start":
                s = self.sessions.get(sid)
                if s and s.current_turn is None:
                    s.current_turn = Turn(index=s.turn_count, started_at=wire.captured_at,
                                          input_text="(resumed)")
                    s.turn_count += 1
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
            session_id=sid, project=self._project_for(cwd), started_at=at,
            last_activity=at, harness=self.profile.name,
            permission_mode=f.get("permission_mode"), cwd=cwd, transcript=f.get("transcript"),
        )
        if self.profile.thread.get("source") == "field":
            s.thread_id = f.get(self.profile.thread.get("id_field"))
        self.sessions[sid] = s

    def _on_session_end(self, sid, f, at) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            self._finalize(s, at)

    def _finalize(self, s: Session, at, incomplete: bool = False) -> None:
        t = s.current_turn
        if t and t.open:
            t.incomplete = incomplete
            self._close_turn(s, at)
        self._emit_pending_turn(s)

    def sweep(self, now: float, ttl: float) -> int:
        """Finalize sessions idle past `ttl` (a harness that crashed before
        session_end), so state can't grow without bound and the pending turn
        still reaches the emitters. Returns how many were swept."""
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
            s.current_turn.steering.append(
                Steering(kind=SteeringKind.INTERJECTION, at=at, text=prompt))
            return
        self._emit_pending_turn(s)            # previous turn is final once the next begins
        s.current_turn = Turn(index=s.turn_count, started_at=at, input_text=prompt)
        s.turn_count += 1

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
        t.open = False
        t.ended_at = at
        # NOT emitted yet: subagents can finish after the harness's Stop, so the
        # turn stays pending until the next turn starts or the session finalizes.

    def _emit_pending_turn(self, s: Session) -> None:
        t = s.current_turn
        if not t or t.open or t.emitted:
            return
        t.emitted = True
        self._thread_of(s)                    # resolve the conversation id once
        if self.enricher:
            try:
                self.enricher.enrich_turn(t, s)   # LLM-call internals from the transcript
            except Exception:
                pass
        for e in self.turn_emitters:
            try:
                e.emit_turn(t, s)
            except Exception:
                pass                          # an emitter must never break the reducer

    def finalize_idle_turns(self, now: float, linger: float) -> int:
        """Emit closed-but-pending turns whose session has been quiet for
        `linger` seconds — so a conversation's LAST turn appears promptly
        instead of waiting for session end or the sweep. Async subagent work
        resets last_activity, so lingering turns still absorb it."""
        n = 0
        for s in self.sessions.values():
            t = s.current_turn
            if t and not t.open and not t.emitted and now - s.last_activity > linger:
                self._emit_pending_turn(s)
                n += 1
        return n

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
        key = f.get("tool_use_id") or f"_synth:{f.get('tool_name')}:{len(t.tool_order)}"
        tc = ToolCall(correlation_key=key, tool_name=f.get("tool_name", "tool"),
                      tool_input=self.redactor.scrub(f.get("tool_input") or {}),
                      started_at=at, agent_id=f.get("agent_id"))
        t.tool_calls[key] = tc
        t.tool_order.append(key)
        if tc.agent_id and tc.agent_id not in t.subagents:
            # interior tool seen before any subagent_start (Claude Code emits none):
            # materialize the subagent record so the tool has a home
            self._open_subagent(t, tc.agent_id, f.get("agent_type") or "agent", at)
        return tc

    def _on_permission_request(self, sid, f, at) -> None:
        # recorded on the tool (prompt shown), not an event of its own
        _, tc = self._locate_tool(sid, f)
        if tc:
            tc.permission = Permission(requested_at=at)

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

    def _open_subagent(self, t: Turn, aid, atype, at, output=None, open=True) -> dict:
        rec = {"agent_id": aid, "type": atype, "started_at": at,
               "open": open, "ended_at": None if open else at, "output": output}
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
        if rec is not None and not rec["open"]:
            return                            # already closed; a repeat stop is noise
        if rec is None:
            if not aid or not f.get("agent_type"):
                return
            self._open_subagent(t, aid, f.get("agent_type"), at, open=False)
            return
        rec["open"] = False
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
        if key and key in t.tool_calls:
            return s, t.tool_calls[key]
        for k in reversed(t.tool_order):          # fallback: last still-running tool
            if t.tool_calls[k].status == ToolStatus.RUNNING:
                return s, t.tool_calls[k]
        return s, None
