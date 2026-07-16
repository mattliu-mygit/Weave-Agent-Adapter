"""Register harness hooks from the profile's `[registration]` contract.

Reads the active profile, emits one command per event, and merges them into the
settings file the profile names, idempotently (re-running replaces our entries;
`uninstall` removes only ours). Every harness's hook file uses the same
`{"hooks": {event: [entry]}}` shape, and the profile declares its own target
paths, so adding a harness needs no code here.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile

from .profile import load_profile

MARKERS = ("weave-agent-adapter hook", "weave_agent_adapter hook")


def _target_path(reg: dict, user: bool) -> str:
    # the profile's [registration] names where its hooks live, per scope
    key = "user_path" if user else "local_path"
    p = reg.get(key)
    if not p:
        raise ValueError(f"profile [registration] is missing {key!r}")
    return os.path.expanduser(p) if user else os.path.join(os.getcwd(), p)


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} must contain valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".hooks-",
                                         suffix=".tmp", delete=False) as f:
            tmp_path = f.name
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _is_ours(entry: dict) -> bool:
    return any(any(marker in h.get("command", "") for marker in MARKERS)
               for h in entry.get("hooks", []))


def _resolved_command(command: str) -> str:
    parts = shlex.split(command)
    if not parts or parts[0] != "weave-agent-adapter":
        return command
    # Pin hooks to the exact console script that invoked installation. Looking
    # it up again on PATH can silently select a different, stale installation.
    invoked = os.path.abspath(sys.argv[0])
    if os.path.basename(invoked) == "weave-agent-adapter" and os.path.isfile(invoked):
        prefix = shlex.quote(invoked)
    else:
        prefix = f"{shlex.quote(sys.executable)} -m weave_agent_adapter"
    suffix = " ".join(shlex.quote(part) for part in parts[1:])
    return f"{prefix} {suffix}" if suffix else prefix


def _entry(command: str, ev: str) -> dict:
    return {"hooks": [{"type": "command", "command": f"{command} --event {ev}"}]}


def install(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    reg = load_profile(harness, profiles_dir).registration
    command, events = _resolved_command(reg["command"]), reg.get("events", [])
    path = path or _target_path(reg, user)
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    for ev in events:
        others = [e for e in hooks.get(ev, []) if not _is_ours(e)]
        others.append(_entry(command, ev))
        hooks[ev] = others
    _write_json(path, data)
    return path


def uninstall(harness: str, user: bool = True, profiles_dir=None, path=None) -> str:
    reg = load_profile(harness, profiles_dir).registration
    path = path or _target_path(reg, user)
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
