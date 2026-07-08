"""Installer (spec 08): wire a harness's hooks from the profile's [registration].

Reads the active profile, emits one command per event, and merges them into the
harness's settings file — idempotently (re-running replaces our entries;
`uninstall` removes only ours). Currently supports the `claude-code-settings`
registration kind; other kinds slot in here as harnesses are added.
"""
from __future__ import annotations

import json
import os

from .profile import load_profile

MARKER = "weave-agent-adapter hook"      # identifies entries we own


def _settings_path(user: bool) -> str:
    if user:
        return os.path.expanduser("~/.claude/settings.json")
    return os.path.join(os.getcwd(), ".claude", "settings.json")


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _is_ours(entry: dict) -> bool:
    return any(MARKER in h.get("command", "") for h in entry.get("hooks", []))


def install(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    prof = load_profile(harness, profiles_dir)
    reg = prof.registration
    if reg.get("kind") != "claude-code-settings":
        raise ValueError(f"unsupported registration kind: {reg.get('kind')!r}")
    command, events = reg["command"], reg.get("events", [])

    path = path or _settings_path(user)
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    for ev in events:
        others = [e for e in hooks.get(ev, []) if not _is_ours(e)]
        others.append({"hooks": [{"type": "command", "command": f"{command} --event {ev}"}]})
        hooks[ev] = others
    _write_json(path, data)
    return path


def uninstall(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    path = path or _settings_path(user)
    data = _read_json(path)
    hooks = data.get("hooks", {})
    for ev in list(hooks.keys()):
        hooks[ev] = [e for e in hooks[ev] if not _is_ours(e)]
        if not hooks[ev]:
            del hooks[ev]
    if hooks == {}:
        data.pop("hooks", None)
    _write_json(path, data)
    return path
