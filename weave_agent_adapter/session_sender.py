"""Send each finished turn to CES as session Cases, off the hook path.

The sidecar calls `handle()` for every wire event. Hook events contribute what the
transcript lacks (permission prompts, permission mode, subagent transcript paths)
and say when a turn has ended. One worker thread reads the transcripts, builds the
turn job, and writes it with `CesWriter`. A job that still fails goes to the DLQ.

Codex and Claude Code have transcript readers. A `Stop` ends a turn: Codex's names
it with `turn_id`, and Claude Code's ends the transcript's last turn unless
background tasks are still running. The idle sweep sends a last turn that was
interrupted. A parent turn's job carries the finished turns of its subagents:
Codex names their transcripts in `SubagentStop`, and Claude Code keeps them in the
session's `subagents` directory.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass, field

from . import claude_transcript, codex_transcript, dlq, records
from .config import session_sampled
from .config_surface import config_version
from .diagnostics import diagnose
from .model import DEFAULT_TRACE_ROLE
from .records import ToolFacts, TurnContext

READERS = {"codex": codex_transcript, "claude-code": claude_transcript}
# Codex writes a turn's task_complete record 35-50 ms after the Stop hook fires (Stop
# hooks can keep a turn going, so the end is recorded after they return). A Stop job
# waits this long for the record. Raise the bound if diagnostics show
# `ces_end_record_timeout`.
END_RECORD_WAIT_S = 2.0
END_RECORD_POLL_S = 0.1


@dataclass
class _Session:
    harness: str
    transcript: str | None = None
    agents: list = field(default_factory=list)     # subagent transcript paths, in arrival order
    last_activity: float = 0.0
    permission_mode: str | None = None
    trace_role: str = DEFAULT_TRACE_ROLE
    cwd: str | None = None
    config_version: str | None = None
    tools: dict = field(default_factory=dict)      # tool_use_id -> ToolFacts


@dataclass
class _Job:
    transcript: str
    turn_id: str | None          # None: the transcript's last turn
    agents: list
    ctx: TurnContext
    facts: dict
    sweep: bool = False          # the session went idle: send every turn that has ended
    final: bool = False          # the session ended: an unended last turn is final too


class SessionSender:
    def __init__(self, writer, dlq_path: str, redactor, adapter_version: str,
                 harnesses=("codex",), config_paths: dict | None = None, session_rate: float = 1.0):
        self.writer = writer
        self.session_rate = session_rate
        self.dlq_path = dlq_path
        self.redactor = redactor
        self.adapter_version = adapter_version
        self.harnesses = set(harnesses) & set(READERS)
        self.config_paths = config_paths or {}
        self.sessions: dict = {}
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0             # jobs queued or running
        self._inflight = None         # (job, key) being written
        self._queued_sweeps: dict = {}  # session id -> its queued sweep job, not started yet
        self._sent: set = set()       # (thread_id, turn_id); worker thread only
        self._abort = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ces-sender", daemon=True)
        self._thread.start()

    # ------- sidecar thread -------

    def handle(self, wire) -> None:
        if wire.harness not in self.harnesses:
            return
        p = wire.payload
        sid = p.get("session_id")
        if not sid or not session_sampled(sid, self.session_rate):
            return
        s = self.sessions.setdefault(sid, _Session(wire.harness))
        s.last_activity = wire.captured_at
        s.trace_role = wire.trace_role
        if p.get("permission_mode"):
            s.permission_mode = p["permission_mode"]
        if p.get("cwd") and not s.cwd:
            s.cwd = p["cwd"]
        if p.get("transcript_path") and not p.get("agent_id"):
            s.transcript = p["transcript_path"]   # subagent events carry their own transcript
        tool_id = p.get("tool_use_id")
        if tool_id:
            facts = s.tools.setdefault(tool_id, ToolFacts())
            facts.hook_tool_name = facts.hook_tool_name or p.get("tool_name")
            if wire.event == "PermissionRequest":
                facts.permission_requested = True
            elif wire.event == "PostToolUse":
                facts.completed = True
        if wire.event == "SubagentStop" and p.get("agent_transcript_path"):
            if p["agent_transcript_path"] not in s.agents:
                s.agents.append(p["agent_transcript_path"])
            if s.transcript:
                # Send the finished subagent now, not at the parent's next Stop or the idle sweep.
                self._enqueue(sid, s, None, sweep=True)
        elif (wire.event == "Stop" and s.transcript and not p.get("background_tasks")
              and (p.get("turn_id") or wire.harness != "codex")):
            self._enqueue(sid, s, p.get("turn_id"))
        elif wire.event == "SessionEnd" and s.transcript:
            self.sessions.pop(sid, None)
            self._enqueue(sid, s, None, sweep=True, final=True)

    def sweep(self, now: float, ttl: float) -> None:
        for sid in [sid for sid, s in self.sessions.items() if now - s.last_activity > ttl]:
            s = self.sessions.pop(sid)
            if s.transcript:
                self._enqueue(sid, s, None, sweep=True)

    def has_active_work(self) -> bool:
        with self._lock:
            return self._pending > 0

    def close(self, timeout: float = 30.0) -> None:
        """Finish queued work for up to `timeout`, then move what is left to the DLQ."""
        self._queue.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            with self._lock:
                self._abort.set()
                inflight = self._inflight
            if inflight:
                job, key = inflight
                dlq.append(self.dlq_path, [dlq.record(job, key, None, "sidecar_exit", 0)])
            # Jobs still queued aren't built, which can take long: each session's transcript
            # goes to the DLQ once, and `evaluations replay` builds its unwritten turns from it.
            rebuilds = {}
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is None:             # the shutdown sentinel
                    continue
                entry = rebuilds.setdefault(job.ctx.session_id, {
                    "transcript": job.transcript, "agents": [], "ctx": asdict(job.ctx), "final": False})
                entry["agents"] = list(dict.fromkeys([*entry["agents"], *job.agents]))
                entry["final"] = entry["final"] or job.final
                with self._lock:
                    self._pending -= 1
            if rebuilds:
                dlq.append(self.dlq_path, [dlq.rebuild_record(entry, "sidecar_exit")
                                           for entry in rebuilds.values()])
            self._queue.put(None)           # so the worker still exits when its write returns
            self._thread.join(5.0)

    def _enqueue(self, sid: str, s: _Session, turn_id, sweep: bool = False, final: bool = False) -> None:
        if s.config_version is None and self.config_paths.get(s.harness):
            s.config_version = config_version(self.config_paths[s.harness], cwd=s.cwd)
        ctx = TurnContext(s.harness, sid, s.permission_mode, s.trace_role, self.adapter_version,
                          s.config_version)
        facts = {k: ToolFacts(v.hook_tool_name, v.permission_requested, v.completed)
                 for k, v in s.tools.items()}
        job = _Job(s.transcript, turn_id, list(s.agents), ctx, facts, sweep, final)
        coalesce = sweep and not final
        with self._lock:
            queued = self._queued_sweeps.get(sid) if coalesce else None
            if queued is not None:
                # The queued sweep reads the newest transcript when it runs, so a burst of
                # SubagentStops costs one job, not one each.
                queued.agents, queued.facts, queued.ctx = job.agents, job.facts, job.ctx
                return
            if coalesce:
                self._queued_sweeps[sid] = job
            self._pending += 1
        self._queue.put(job)

    # ------- worker thread -------

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            self._run_job(job)

    def _run_job(self, job: _Job) -> None:
        with self._lock:
            if self._queued_sweeps.get(job.ctx.session_id) is job:
                del self._queued_sweeps[job.ctx.session_id]
        try:
            self._process(job)
        except Exception as error:
            diagnose("ces_build", harness=job.ctx.harness, error=error)
            if job.transcript:
                # The transcript still holds the turns, so `evaluations replay` can build them again.
                dlq.append(self.dlq_path, [dlq.rebuild_record(
                    {"transcript": job.transcript, "agents": list(job.agents), "ctx": asdict(job.ctx),
                     "final": job.final}, type(error).__name__)])
        finally:
            with self._lock:
                self._pending -= 1

    def _turns_to_send(self, t, job: _Job, reader) -> list:
        ends = reader.turn_ends(t)
        ids = [turn_id for turn_id, _ in ends]
        thread_id = reader.thread_id(t)
        written = self.writer.written_turns(job.ctx.harness, job.ctx.session_id)
        if job.sweep:
            # Send every turn that has ended, such as one that was interrupted or whose Stop
            # came while background tasks still ran. A turn followed by another has ended. An
            # unended last turn may still be running a long tool call, and sending it now would
            # freeze it as incomplete under its only key, so it waits for its Stop unless the
            # session ended.
            last_open = bool(ends) and ends[-1][1] is None and not job.final
            sendable = ids[:-1] if last_open else ids
            target = sendable[-1] if sendable else None
        else:
            target = job.turn_id or (ids[-1] if ids else None)
            if target not in ids:
                diagnose("ces_turn_missing", harness=job.ctx.harness)
                return []
        if target is None:
            return []
        # A turn can end without a Stop that sends it: an interrupted turn gets none, and a
        # Claude Code Stop with background tasks pending is skipped. Send those first.
        return [turn_id for turn_id in ids[:ids.index(target) + 1]
                if turn_id not in written and (thread_id, turn_id) not in self._sent]

    def _load(self, job: _Job, reader):
        """Load the transcript. A Stop job waits briefly for its turn to be written as ended."""
        t = reader.load(job.transcript)
        if job.sweep:
            return t
        deadline = time.monotonic() + END_RECORD_WAIT_S
        while (_turn_open(t, job, reader) is not False and time.monotonic() < deadline
               and not self._abort.wait(END_RECORD_POLL_S)):
            t = reader.load(job.transcript)
        if _turn_open(t, job, reader):
            diagnose("ces_end_record_timeout", harness=job.ctx.harness)
        return t

    def _agent_turns(self, job: _Job, t, reader, pending: list, sites: dict) -> list:
        # Subagent turns are deduplicated by what the session file records as written, not by
        # _sent, because the writer defers a subagent whose launching turn isn't written yet.
        written = self.writer.written_turns(job.ctx.harness, job.ctx.session_id)
        # With no parent turn to write first, a subagent the writer can't root yet would only be
        # deferred, so its turns aren't built. A written launching turn lets the writer root it.
        only = (None if pending or job.final
                else self.writer.rootable_agents(job.ctx.harness, job.ctx.session_id)
                | {agent for agent, (turn_id, _) in sites.items() if turn_id in written})
        return [(key, turn) for key, turn in
                agent_turns(reader, t, job.agents, job.facts, job.ctx, self.redactor, self._sent,
                            final=job.final, only=only)
                if key[1] not in written]

    def _process(self, job: _Job) -> None:
        reader = READERS[job.ctx.harness]
        t = self._load(job, reader)
        thread_id = reader.thread_id(t)
        pending = [turn_id for turn_id in self._turns_to_send(t, job, reader)
                   if (thread_id, turn_id) not in self._sent]
        sites = reader.launch_sites(t)
        agents = self._agent_turns(job, t, reader, pending, sites)
        if not pending and not agents:
            return
        ends = reader.turn_ends(t)
        if not ends:
            diagnose("ces_turn_missing", harness=job.ctx.harness)
            return
        first = reader.select_turn(t, ends[0][0])
        session_case = reader.session_case(t, first, job.ctx)
        session_case = {**session_case,
                        "input": {k: self.redactor.scrub(v) for k, v in session_case["input"].items()}}
        prefix = {"harness": job.ctx.harness, "session_id": job.ctx.session_id, "session_case": session_case,
                  "launch_sites": _sites_for([turn for _, turn in agents], sites)}
        for number, turn_id in enumerate(pending):
            turn = reader.select_turn(t, turn_id)
            riders = agents if number == len(pending) - 1 else []
            body = {**prefix, "turns": [reader.turn_records(t, turn, job.facts, job.ctx, self.redactor),
                                        *(agent_turn for _, agent_turn in riders)]}
            self._write(body, f"turn-v1:{job.ctx.harness}:{job.ctx.session_id}:{turn_id}", job.final)
            self._sent.add((thread_id, turn_id))
        if not pending:
            # Subagent turns that finished after their parent's last turn was sent go on their own,
            # each under its own key, because the parent turn's key already holds another body.
            for key, agent_turn in agents:
                self._write({**prefix, "turns": [agent_turn]},
                            f"turn-v1:{job.ctx.harness}:{job.ctx.session_id}:{key[1]}", job.final)

    def _write(self, job: dict, key: str, final: bool = False) -> None:
        with self._lock:
            aborted = self._abort.is_set()
            if not aborted:
                self._inflight = (job, key)
        if aborted:
            dlq.append(self.dlq_path, [dlq.record(job, key, None, "sidecar_exit", 0)])
            return
        try:
            self.writer.write_turn(job, final=final)
        except Exception as error:
            status, code = getattr(error, "status", None), getattr(error, "code", None)
            diagnose("ces_turn_too_large" if status == 413 else "ces_send", harness=job["harness"], error=error)
            dlq.append(self.dlq_path, [dlq.record(job, key, status, code or type(error).__name__,
                                                  getattr(error, "attempts", 1))])
        finally:
            with self._lock:
                self._inflight = None


def _turn_open(t, job: _Job, reader):
    """Whether a Stop job's turn is still unended, or None while the transcript lacks it.

    A Codex Stop names its turn; a Claude Code Stop ends the transcript's last turn."""
    ends = dict(reader.turn_ends(t))
    turn_id = job.turn_id if job.turn_id is not None else next(reversed(ends), None)
    return ends[turn_id] is None if turn_id in ends else None


def _sites_for(turns: list, sites: dict) -> dict:
    """The launch sites of the subagents these turns belong to, so the writer can root them."""
    return {turn["agent_id"]: sites[turn["agent_id"]] for turn in turns if turn["agent_id"] in sites}


def agent_turns(reader, t, paths, facts, ctx, redactor, sent=frozenset(), final=False, only=None) -> list:
    """Finished, unsent subagent turns as ((thread_id, turn_id), job turn), parents first.
    With `only`, just the subagents whose thread IDs it holds."""
    loaded = []
    for path in dict.fromkeys([*paths, *reader.subagent_paths(t)]):
        try:
            loaded.append(reader.load(path))
        except (OSError, ValueError) as error:
            diagnose("ces_agent_transcript", harness=ctx.harness, error=error)
    parents = {reader.thread_id(a): reader.parent_thread_id(a) for a in loaded}

    def depth(thread, seen=()):
        parent = parents.get(thread)
        return 0 if parent not in parents or thread in seen else 1 + depth(parent, (*seen, thread))

    found = []
    for a in loaded:
        thread = reader.thread_id(a)
        if only is not None and thread not in only:
            continue
        for turn_id, end in reader.turn_ends(a):
            if (end is None and not final) or (thread, turn_id) in sent:
                continue
            turn = reader.select_turn(a, turn_id)
            found.append((depth(thread), records.ms(turn.rows[0].get("timestamp")) or 0, (thread, turn_id),
                          reader.turn_records(a, turn, facts, ctx, redactor)))
    found.sort(key=lambda f: (f[0], f[1]))
    return [(key, turn) for _, _, key, turn in found]


def transcript_jobs(reader, t, ctx: TurnContext, redactor, agents=(), final=True) -> list:
    """Every turn of a loaded transcript as (key, job), oldest first.

    For a session that is over (`final`), every turn is final, as in the idle sweep;
    otherwise an unended last turn may still be running and is left out. Subagent turns,
    from the transcript's own subagent files and `agents`, ride on the last turn's job."""
    ends = reader.turn_ends(t)
    if ends and ends[-1][1] is None and not final:
        ends = ends[:-1]
    turn_ids = [turn_id for turn_id, _ in ends]
    if not turn_ids:
        return []
    session_case = reader.session_case(t, reader.select_turn(t, turn_ids[0]), ctx)
    session_case = {**session_case, "input": {k: redactor.scrub(v) for k, v in session_case["input"].items()}}
    jobs = []
    for number, turn_id in enumerate(turn_ids):
        turns = [reader.turn_records(t, reader.select_turn(t, turn_id), {}, ctx, redactor)]
        if number == len(turn_ids) - 1:
            turns += [turn for _, turn in agent_turns(reader, t, list(agents), {}, ctx, redactor, final=final)]
        body = {"harness": ctx.harness, "session_id": ctx.session_id, "session_case": session_case,
                "turns": turns, "launch_sites": _sites_for(turns, reader.launch_sites(t))}
        jobs.append((f"turn-v1:{ctx.harness}:{ctx.session_id}:{turn_id}", body))
    return jobs
