"""Load a harness profile (TOML): how to install its hooks, where a hook payload
holds the session's working directory, and which files make up config_version.

Hook events and transcripts are read by code, in the session sender and the
transcript readers, not through the profile. Field paths are dotted (e.g. "a.b").
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
    fields: dict          # [fields]: field -> dotted path in a hook payload; the hook reads `cwd`
    registration: dict
    config_surface: dict = None   # [config_surface]: artifact paths hashed into config_version

    def __post_init__(self):
        self.config_surface = self.config_surface or {}

    def extract(self, payload: dict) -> dict:
        out = {}
        for name, path in self.fields.items():
            val = _dig(payload, path) if path else None
            if val is not None:
                out[name] = val
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        h = d.get("harness", {})
        return cls(
            name=h.get("name", "unknown"),
            fields=dict(d.get("fields", {})),
            registration=dict(d.get("registration", {})),
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
