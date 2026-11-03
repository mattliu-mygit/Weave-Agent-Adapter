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

import json
import os
import re
import urllib.parse

from .diagnostics import diagnose

DEFAULT_STATE_DIR = os.path.expanduser(os.environ.get(
    "WEAVE_AGENT_ADAPTER_EVALUATIONS_SESSIONS", "~/.weave-agent-adapter/evaluations-sessions"))


def already_stored(error) -> bool:
    """CES holds a different body under this key, which only an earlier write of this turn sent."""
    return getattr(error, "status", None) == 409 and getattr(error, "code", None) == "idempotency_key_reuse"


class CesWriter:
    def __init__(self, client, scope_ref: str, dataset_id: str, state_dir: str | None = None,
                 namespace: str = "wandb"):
        scope = urllib.parse.quote(scope_ref, safe="")
        base = f"/v1/namespaces/{namespace}/scopes/{scope}/datasets/{dataset_id}"
        self.cases_path, self.trajectories_path = base + "/cases", base + "/trajectories"
        self.client = client
        self.state_dir = state_dir or DEFAULT_STATE_DIR

    def write_turn(self, job: dict) -> None:
        """Raises the CesError of the first request that fails; the caller keeps the whole job."""
        harness, sid = job["harness"], job["session_id"]
        state = self._load(harness, sid)
        s = self._session_trajectory(job, state)
        for turn in job["turns"]:
            agent = turn["agent_id"]
            if agent is not None and agent not in state["agents"]:
                root = state["agent_roots"].get(agent)
                if root is None:
                    diagnose("ces_subagent_root_missing", harness=harness)
                    root = state["session_case"]["id"]
                parent = state["agents"].get(turn["parent_agent_id"]) or s
                body = {"trajectories": [{"name": f"{harness} subagent {agent}", "root_case_id": root,
                                          "parent_trajectory_id": parent}]}
                result = self.client.post(self.trajectories_path, body, f"subagent-v1:{harness}:{sid}:{agent}")
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
            except Exception as error:
                if not already_stored(error):
                    raise
                diagnose("ces_already_stored", harness=harness)
                result = None
            for spawned, index in turn["spawns"].items() if result else ():
                state["agent_roots"].setdefault(spawned, result["case_ids"][index])
            state["turns"].append(turn["turn_id"])
            self._save(harness, sid, state)

    def written_turns(self, harness: str, sid: str) -> set:
        """Turn IDs this session's file records as written, so a restart does not resend them."""
        return set(self._load(harness, sid)["turns"])

    def _session_trajectory(self, job: dict, state: dict) -> str:
        harness, sid = job["harness"], job["session_id"]
        for name, path, key, body in (
            ("session_case", self.cases_path, f"session-v1:{harness}:{sid}",
             lambda: {"cases": [job["session_case"]]}),
            ("session_trajectory", self.trajectories_path, f"session-trajectory-v1:{harness}:{sid}",
             lambda: {"trajectories": [{"name": f"{harness} session {sid}",
                                        "root_case_id": state["session_case"]["id"]}]}),
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

    def _path(self, harness: str, sid: str) -> str:
        return os.path.join(self.state_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", f"{harness}-{sid}") + ".json")

    def _load(self, harness: str, sid: str) -> dict:
        try:
            with open(self._path(harness, sid), encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        for name in ("session_case", "session_trajectory", "agents", "agent_roots"):
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
