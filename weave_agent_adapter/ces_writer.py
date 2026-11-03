"""Write one turn job to CES in order: the session Case and Trajectory, then the turn's Cases.

Every request carries an Idempotency-Key built from what it writes, and a replay
sends a byte-identical body, so CES answers a repeat with the original IDs. The
per-session file keeps the IDs, and each body before it is sent: after a crash
between sending and saving, resending the stored body recovers the ID.

A turn job is a JSON object (`codex_transcript.turn_records` builds its turns):

    {"harness", "session_id", "session_case": {"input", "output"},
     "turns": [{"agent_id", "parent_agent_id", "turn_id", "cases": [...], "spawns": {agent: case index}}]}

The main thread's turn comes first, then subagent turns, parents first.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import urllib.parse

from .diagnostics import diagnose

DEFAULT_STATE_DIR = os.path.expanduser(os.environ.get(
    "WEAVE_AGENT_ADAPTER_EVALUATIONS_SESSIONS", "~/.weave-agent-adapter/evaluations-sessions"))


def rollout_context(role: str, dataset_id: str, record_key: str) -> dict:
    """CES's rollout_context v1 for a new Trajectory. References the adapter lacks are null.

    Workaround until wandb/core#60301 deploys: CES rejects or ignores the field before then,
    and a Trajectory can't get a context after creation, so `[evaluations] rollout_context`
    stays off until every CES the adapter writes to serves it. Then make it the default and
    delete the flag."""
    return {"protocol_version": 1, "role": role, "experiment_dataset_id": dataset_id,
            "client_record_key": record_key, "evaluated_trajectory_id": None,
            "models_run_ref": None, "trace_ref": None}


def already_stored(error) -> bool:
    """CES holds a different body under this key, which only an earlier write of this turn sent."""
    return getattr(error, "status", None) == 409 and getattr(error, "code", None) == "idempotency_key_reuse"


class SubagentDeferred(Exception):
    """Subagent turns still wait for the Case IDs of the turn that launched them, and no later
    job will retry them, so the job goes to the dead-letter file for `evaluations replay`."""
    status = None
    code = "subagent_deferred"

    def __init__(self, turn_ids: list):
        super().__init__(f"{len(turn_ids)} subagent turn(s) wait for the turn that launched them")
        self.turn_ids = turn_ids


class CesWriter:
    def __init__(self, client, scope_ref: str, dataset_id: str, state_dir: str | None = None,
                 namespace: str = "wandb", rollout_context: bool = False):
        scope = urllib.parse.quote(scope_ref, safe="")
        self.scope_path = f"/v1/namespaces/{namespace}/scopes/{scope}"
        base = f"{self.scope_path}/datasets/{dataset_id}"
        self.cases_path, self.trajectories_path = base + "/cases", base + "/trajectories"
        self.client = client
        self.dataset_id = dataset_id
        self.rollout_context = rollout_context
        self.state_dir = state_dir or DEFAULT_STATE_DIR

    def write_turn(self, job: dict, final: bool = False) -> None:
        """Raises the CesError of the first request that fails; the caller keeps the whole job.

        A subagent whose launching turn isn't written yet waits for a later job. A final job has
        none, so with `final` the subagent turns that still wait raise SubagentDeferred at the end."""
        harness, sid = job["harness"], job["session_id"]
        with self._locked(harness, sid):
            deferred = self._write_locked(job, harness, sid)
        if final and deferred:
            raise SubagentDeferred(deferred)

    def _write_locked(self, job: dict, harness: str, sid: str) -> list:
        state = self._load(harness, sid)
        s = self._session_trajectory(job, state)
        deferred = []
        for turn in job["turns"]:
            agent = turn["agent_id"]
            if agent is not None and agent not in state["agents"]:
                root = state["agent_roots"].get(agent) or self._retry_root(state, agent)
                parent = s if turn["parent_agent_id"] is None else state["agents"].get(turn["parent_agent_id"])
                if root is None or parent is None:
                    # CES roots a subagent's Trajectory at a Case of its parent's Trajectory, so the
                    # subagent waits until the turn that launched it is written; a later job sends it.
                    deferred.append(turn["turn_id"])
                    continue
                key = f"subagent-v1:{harness}:{sid}:{agent}"
                body = {"trajectories": [self._trajectory(
                    {"name": f"{harness} subagent {agent}", "root_case_id": root, "parent_trajectory_id": parent},
                    "child_agent", key)]}
                result = self.client.post(self.trajectories_path, body, key)
                state["agents"][agent] = result["trajectory_ids"][0]
                self._save(harness, sid, state)
            trajectory = s if agent is None else state["agents"][agent]
            cases = [{**case, "input": {**case["input"], "session_trajectory_id": s}, "trajectory_id": trajectory}
                     for case in turn["cases"]]
            # One request per turn. CES takes at most 10,000 Cases and a 16 MiB body, which a
            # turn has never come near. If 413 diagnostics appear, split the turn into ordered parts.
            try:
                result = self.client.patch(self.cases_path, {"cases": cases},
                                           f"turn-v1:{harness}:{sid}:{turn['turn_id']}")
                ids = result.get("case_ids", [])
                roots = {spawned: ids[index] for spawned, index in turn["spawns"].items()}
            except Exception as error:
                if not already_stored(error):
                    raise
                diagnose("ces_already_stored", harness=harness)
                # The stored turn's Case IDs still root the subagents it launched. If they can't be
                # read now, the session file keeps where to look, and a later job tries again.
                steps = self._stored_steps(trajectory, turn["turn_id"]) if turn["spawns"] else {}
                roots = {}
                for spawned, index in turn["spawns"].items():
                    step = turn["cases"][index]["input"].get("step_index")
                    if (steps or {}).get(step):
                        roots[spawned] = steps[step]
                    else:
                        state["unrooted"][spawned] = [trajectory, turn["turn_id"], step]
            for spawned, root in roots.items():
                state["agent_roots"].setdefault(spawned, root)
            state["turns"].append(turn["turn_id"])
            self._save(harness, sid, state)
        if deferred:
            diagnose("ces_subagent_deferred", harness=harness)
        return deferred

    def _retry_root(self, state: dict, agent: str) -> str | None:
        """Read the launching Case of a subagent whose root an earlier 409 couldn't read back."""
        site = state["unrooted"].get(agent)
        if site is None:
            return None
        trajectory, turn_id, step = site
        root = (self._stored_steps(trajectory, turn_id) or {}).get(step)
        if root is not None:
            state["agent_roots"][agent] = root
            del state["unrooted"][agent]
        return root

    def _stored_steps(self, trajectory: str, turn_id: str) -> dict | None:
        """The Case IDs CES already holds for a turn, by step index, or None when unreadable."""
        by_step, cursor = {}, None
        try:
            while True:
                query = "?limit=1000" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
                page = self.client.request("GET", f"{self.scope_path}/trajectories/{trajectory}/cases{query}", None)
                for case in page.get("cases", []):
                    if (case.get("input") or {}).get("turn_id") == turn_id:
                        by_step[case["input"].get("step_index")] = case["case_id"]
                cursor = page.get("next_cursor")
                if not cursor:
                    break
        except Exception:
            diagnose("ces_stored_cases_unreadable")
            return None
        return by_step

    @contextlib.contextmanager
    def _locked(self, harness: str, sid: str):
        """Hold the session's lock, so `evaluations replay` and the live sidecar can't each save
        the session file over the other's changes."""
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        fd = os.open(self._path(harness, sid) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def rootable_agents(self, harness: str, sid: str) -> set:
        """Subagents this session's file can root now or already holds Trajectories for."""
        state = self._load(harness, sid)
        return set(state["agents"]) | set(state["agent_roots"]) | set(state["unrooted"])

    def written_turns(self, harness: str, sid: str) -> set:
        """Turn IDs this session's file records as written, so a restart does not resend them."""
        return set(self._load(harness, sid)["turns"])

    def session_ids(self, harness: str, sid: str) -> dict:
        """The session Case and Trajectory IDs this session's file records."""
        state = self._load(harness, sid)
        return {"session_case_id": state["session_case"].get("id"),
                "session_trajectory_id": state["session_trajectory"].get("id")}

    def _session_trajectory(self, job: dict, state: dict) -> str:
        harness, sid = job["harness"], job["session_id"]
        for name, path, key, body in (
            ("session_case", self.cases_path, f"session-v1:{harness}:{sid}",
             lambda: {"cases": [job["session_case"]]}),
            ("session_trajectory", self.trajectories_path, f"session-trajectory-v1:{harness}:{sid}",
             lambda: {"trajectories": [self._trajectory(
                 {"name": f"{harness} session {sid}", "root_case_id": state["session_case"]["id"]},
                 "evaluated_rollout", f"session-trajectory-v1:{harness}:{sid}")]}),
        ):
            entry = state[name]
            if entry.get("id"):
                continue
            if entry.get("body") is None:
                entry["body"] = body()
                self._save(harness, sid, state)
            send = self.client.patch if path == self.cases_path else self.client.post
            result = send(path, entry["body"], key)
            entry["id"] = (result.get("case_ids") or result.get("trajectory_ids"))[0]
            self._save(harness, sid, state)
        return state["session_trajectory"]["id"]

    def _trajectory(self, entry: dict, role: str, key: str) -> dict:
        if not self.rollout_context:
            return entry
        return {**entry, "rollout_context": rollout_context(role, self.dataset_id, key)}

    def _path(self, harness: str, sid: str) -> str:
        return os.path.join(self.state_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", f"{harness}-{sid}") + ".json")

    def _load(self, harness: str, sid: str) -> dict:
        try:
            with open(self._path(harness, sid), encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        for name in ("session_case", "session_trajectory", "agents", "agent_roots", "unrooted"):
            state.setdefault(name, {})
        state.setdefault("turns", [])
        return state

    def _save(self, harness: str, sid: str, state: dict) -> None:
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        path = self._path(harness, sid)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(path + ".tmp", path)
