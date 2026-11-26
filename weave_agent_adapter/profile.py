"""Load a harness profile (TOML): how to install its hooks and which files make up
config_version.

Hook payloads and transcripts are read by code, in the hook, the session sender,
and the transcript readers, not through the profile.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:
    import tomllib as _toml            # py 3.11+
except ModuleNotFoundError:            # py < 3.11
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


DEFAULT_PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")


@dataclass
class Profile:
    name: str
    registration: dict
    config_surface: dict = None   # [config_surface]: artifact paths hashed into config_version

    def __post_init__(self):
        self.config_surface = self.config_surface or {}

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        h = d.get("harness", {})
        return cls(
            name=h.get("name", "unknown"),
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
