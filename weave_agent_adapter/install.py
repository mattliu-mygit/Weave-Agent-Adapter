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

# registration kind -> (user-scope path, project-scope path). Both files use the
# same {"hooks": {event: [entry]}} shape, so one merge covers every harness.
_TARGETS = {
    "claude-code-settings": ("~/.claude/settings.json", ".claude/settings.json"),
    "codex-hooks":          ("~/.codex/hooks.json",     ".codex/hooks.json"),
}


def _settings_path(kind: str, user: bool) -> str:
    if kind not in _TARGETS:
        raise ValueError(f"unsupported registration kind: {kind!r}")
    user_path, local_path = _TARGETS[kind]
    return os.path.expanduser(user_path) if user else os.path.join(os.getcwd(), local_path)


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


def _entry(command: str, ev: str) -> dict:
    return {"hooks": [{"type": "command", "command": f"{command} --event {ev}"}]}


def _registration(harness: str, profiles_dir=None):
    reg = load_profile(harness, profiles_dir).registration
    return reg["command"], reg.get("events", []), reg.get("kind")


def install(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    command, events, kind = _registration(harness, profiles_dir)
    path = path or _settings_path(kind, user)
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    for ev in events:
        others = [e for e in hooks.get(ev, []) if not _is_ours(e)]
        others.append(_entry(command, ev))
        hooks[ev] = others
    _write_json(path, data)
    return path


def write_plugin(harness: str, dest: str, profiles_dir=None) -> str:
    """Emit a Claude Code plugin dir (manifest + hooks.json) for zero-config install.

    Same per-event commands as `install`, but packaged so a user adds the plugin
    once instead of editing settings.json — the hooks auto-register on load.
    """
    command, events, _ = _registration(harness, profiles_dir)
    manifest = {
        "name": "weave-agent-adapter",
        "description": "Trace agent-harness sessions to W&B Weave (session/turn/tool/permission).",
        "version": "0.1.0",
    }
    hooks = {ev: [_entry(command, ev)] for ev in events}
    _write_json(os.path.join(dest, ".claude-plugin", "plugin.json"), manifest)
    _write_json(os.path.join(dest, "hooks", "hooks.json"), {"hooks": hooks})
    return dest


def uninstall(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    _, _, kind = _registration(harness, profiles_dir)
    path = path or _settings_path(kind, user)
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
