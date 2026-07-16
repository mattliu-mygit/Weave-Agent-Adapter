"""Load command-hook harness profiles and extract canonical fields.

Loads a declarative profile (TOML) and turns raw harness payloads into
canonical events + fields, so the sidecar holds no harness-specific
knowledge. Field extraction resolves dotted paths (e.g. "tool_input.command").
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

try:
    import tomllib as _toml            # py 3.11+
except ModuleNotFoundError:            # py < 3.11
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


DEFAULT_PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


@dataclass
class Profile:
    name: str
    events: dict          # native event -> canonical action
    fields: dict          # canonical field -> dotted path in payload
    registration: dict
    thread: dict = None      # [thread]: how to derive the conversation id
    enrich: dict = None      # [enrich]: named turn-enrichment strategy (LLM-call internals)
    config_surface: dict = None   # [config_surface]: artifact paths hashed into config_version

    def __post_init__(self):
        self.thread = self.thread or {}
        self.enrich = self.enrich or {}
        self.config_surface = self.config_surface or {}

    def canonical_event(self, native_event: str) -> Optional[str]:
        return self.events.get(native_event)

    def field(self, payload: dict, canonical: str) -> Any:
        path = self.fields.get(canonical)
        return _dig(payload, path) if path else None

    def extract(self, payload: dict) -> dict:
        out = {}
        for canonical in self.fields:
            val = self.field(payload, canonical)
            if val is not None:
                out[canonical] = val
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        h = d.get("harness", {})
        return cls(
            name=h.get("name", "unknown"),
            events=dict(d.get("events", {})),
            fields=dict(d.get("fields", {})),
            registration=dict(d.get("registration", {})),
            thread=dict(d.get("thread", {})),
            enrich=dict(d.get("enrich", {})),
            config_surface=dict(d.get("config_surface", {})),
        )


def load_profile(name: str, profiles_dir: Optional[str] = None) -> Profile:
    """Load `profiles/<name>.toml` (or an explicit .toml path)."""
    if _toml is None:
        raise RuntimeError("No TOML parser available; `pip install tomli` on Python < 3.11")
    directory = profiles_dir or DEFAULT_PROFILES_DIR
    path = name if name.endswith(".toml") else os.path.join(directory, f"{name}.toml")
    with open(path, "rb") as f:
        return Profile.from_dict(_toml.load(f))
