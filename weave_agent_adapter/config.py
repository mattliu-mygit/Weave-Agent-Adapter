"""Sidecar configuration and authoritative Weave project routing.

The hook stays config-free; it just spawns the sidecar, which loads this. TOML
parsing needs `tomli` on Python < 3.11 (a sidecar-only dependency).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

try:
    import tomllib as _toml            # py 3.11+
except ModuleNotFoundError:
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None

DEFAULT_PATH = os.path.expanduser(
    os.environ.get("WEAVE_AGENT_ADAPTER_CONFIG", "~/.weave-agent-adapter/config.toml")
)
# IDs written by `weave-agent-adapter evaluations setup`; the stdlib cannot write TOML.
EVALUATIONS_STATE_PATH = os.path.expanduser(os.environ.get(
    "WEAVE_AGENT_ADAPTER_EVALUATIONS_STATE", "~/.weave-agent-adapter/evaluations.json"))


def resolve_project(configured: str, cwd=None, per_repo: bool = False) -> str:
    """Resolve the project route once, preserving an explicitly chosen entity."""
    entity, separator, project = configured.partition("/")
    if not separator:
        entity, project = "", configured
    if per_repo and cwd:
        leaf = os.path.basename(os.path.normpath(cwd))
        project = re.sub(r"[^A-Za-z0-9_.-]", "-", leaf).strip("-") or project
    return f"{entity}/{project}" if entity else project

@dataclass
class EvaluationsConfig:
    """Where `[evaluations]` sends session Cases. Disabled unless configured."""
    enabled: bool = False
    base_url: str = "https://evaluations.wandb.ai"
    wandb_base_url: str = "https://api.wandb.ai"      # authorizes CES; its ~/.netrc entry holds the key
    project: str = ""                                 # "entity/project" on wandb_base_url
    name: str = "agent-sessions"                      # the CES Dataset that holds the sessions
    harnesses: tuple = ("claude-code", "codex", "gemini-cli")
    rollout_context: bool = False                     # turn on once CES serves wandb/core#60301


@dataclass
class Config:
    project: str = "weave-agent-adapter"   # "entity/project" or bare "project"
    project_per_repo: bool = False         # trace each repo (cwd leaf) to its own project
    redact_enabled: bool = True
    redact_keys: list = None               # None -> Redactor defaults
    session_rate: float = 1.0
    idle_shutdown_s: float = 120.0
    session_ttl_s: float = 3600.0          # drop sessions idle past this (crash safety)
    evaluations: EvaluationsConfig = field(default_factory=EvaluationsConfig)


def _load_file(path: str) -> dict:
    if _toml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return _toml.load(f)
    except Exception:
        return {}


def load_config(path=None) -> Config:
    d = _load_file(path or DEFAULT_PATH)
    weave = d.get("weave", {})
    red = d.get("redaction", {})
    samp = d.get("sampling", {})
    side = d.get("sidecar", {})
    c = Config()

    c.project = os.environ.get("WEAVE_PROJECT", weave.get("project", c.project))
    entity = os.environ.get("WANDB_ENTITY", weave.get("entity"))
    if entity and "/" not in c.project:
        c.project = f"{entity}/{c.project}"
    c.project_per_repo = bool(weave.get("project_per_repo", c.project_per_repo))
    c.redact_enabled = bool(red.get("enabled", c.redact_enabled))
    c.redact_keys = red.get("redact_keys", None)
    c.session_rate = float(samp.get("session_rate", c.session_rate))
    c.idle_shutdown_s = float(os.environ.get("WEAVE_AGENT_ADAPTER_IDLE_S",
                                             side.get("idle_shutdown_s", c.idle_shutdown_s)))
    c.session_ttl_s = float(side.get("session_ttl_s", c.session_ttl_s))
    ev = d.get("evaluations", {})
    c.evaluations = EvaluationsConfig(
        enabled=bool(ev.get("enabled", False)),
        base_url=str(ev.get("base_url", EvaluationsConfig.base_url)),
        wandb_base_url=str(ev.get("wandb_base_url", EvaluationsConfig.wandb_base_url)),
        project=str(ev.get("project", "")),
        name=str(ev.get("name", EvaluationsConfig.name)),
        harnesses=tuple(ev.get("harnesses", EvaluationsConfig.harnesses)),
        rollout_context=bool(ev.get("rollout_context", False)),
    )
    return c


def load_evaluations_state(path=None):
    """`{"project", "name", "scope_ref", "dataset_id"}`, or None before setup."""
    try:
        with open(path or EVALUATIONS_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_evaluations_state(state: dict, path=None) -> None:
    path = path or EVALUATIONS_STATE_PATH
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)
