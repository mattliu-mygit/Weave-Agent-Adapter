"""Config (spec 07): env > config.toml > defaults. Read by the sidecar only.

The hook stays config-free; it just spawns the sidecar, which loads this. TOML
parsing needs `tomli` on Python < 3.11 (a sidecar-only dependency).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

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


@dataclass
class Config:
    active_harness: str = "claude-code"
    project: str = "weave-agent-adapter"   # "entity/project" or bare "project"
    enable_disk_fallback: bool = True      # SDK dead-letter log for sends that fail after retries
    redact_enabled: bool = True
    redact_keys: list = None               # None -> Redactor defaults
    session_rate: float = 1.0
    granularity: str = "session"
    idle_shutdown_s: float = 120.0


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
    trace = d.get("trace", {})
    side = d.get("sidecar", {})
    c = Config()

    c.active_harness = os.environ.get("WEAVE_AGENT_ADAPTER_HARNESS", d.get("active_harness", c.active_harness))
    c.project = os.environ.get("WEAVE_PROJECT", weave.get("project", c.project))
    entity = os.environ.get("WANDB_ENTITY", weave.get("entity"))
    if entity and "/" not in c.project:
        c.project = f"{entity}/{c.project}"
    c.enable_disk_fallback = bool(weave.get("enable_disk_fallback", c.enable_disk_fallback))
    c.redact_enabled = bool(red.get("enabled", c.redact_enabled))
    c.redact_keys = red.get("redact_keys", None)
    c.session_rate = float(samp.get("session_rate", c.session_rate))
    c.granularity = trace.get("granularity", c.granularity)
    c.idle_shutdown_s = float(os.environ.get("WEAVE_AGENT_ADAPTER_IDLE_S",
                                             side.get("idle_shutdown_s", c.idle_shutdown_s)))
    return c
