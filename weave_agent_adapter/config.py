"""Sidecar configuration.

The hook stays config-free; it just spawns the sidecar, which loads this. TOML
parsing needs `tomli` on Python < 3.11 (a sidecar-only dependency).
"""
from __future__ import annotations

import hashlib
import json
import os
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


def session_sampled(session_id: str, rate: float) -> bool:
    """`[sampling] session_rate`, decided per session ID, so a session is all in or all out."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return int.from_bytes(hashlib.md5(session_id.encode()).digest()[:4], "big") / 2 ** 32 < rate


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


def _number(value, default: float) -> float:
    """A numeric setting, or its default when missing or unreadable, so a typo can't stop the sidecar."""
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def load_config(path=None) -> Config:
    d = _load_file(path or DEFAULT_PATH)
    red = d.get("redaction", {})
    samp = d.get("sampling", {})
    side = d.get("sidecar", {})
    c = Config()

    c.redact_enabled = bool(red.get("enabled", c.redact_enabled))
    c.redact_keys = red.get("redact_keys", None)
    c.session_rate = _number(samp.get("session_rate"), c.session_rate)
    c.idle_shutdown_s = _number(os.environ.get("WEAVE_AGENT_ADAPTER_IDLE_S", side.get("idle_shutdown_s")),
                                c.idle_shutdown_s)
    c.session_ttl_s = _number(side.get("session_ttl_s"), c.session_ttl_s)
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
