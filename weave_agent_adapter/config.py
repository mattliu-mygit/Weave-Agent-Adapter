"""Sidecar configuration and authoritative Weave project routing.

The hook stays config-free; it just spawns the sidecar, which loads this. TOML
parsing needs `tomli` on Python < 3.11 (a sidecar-only dependency).
"""
from __future__ import annotations

import os
import re
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
class Config:
    project: str = "weave-agent-adapter"   # "entity/project" or bare "project"
    project_per_repo: bool = False         # trace each repo (cwd leaf) to its own project
    redact_enabled: bool = True
    redact_keys: list = None               # None -> Redactor defaults
    session_rate: float = 1.0
    idle_shutdown_s: float = 120.0
    session_ttl_s: float = 3600.0          # drop sessions idle past this (crash safety)


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
    return c
