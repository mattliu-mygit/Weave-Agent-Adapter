"""Codex exercises the same reducer through its declarative profile.

Codex exposes explicit subagent lifecycle events and no SessionEnd; Stop emits
a normal turn immediately, while the TTL sweep remains crash safety for turns
that never receive Stop.
"""
from __future__ import annotations

import json

from weave_agent_adapter.install import install

CX = "codex"
SID = "cx1"




def test_codex_install_targets_codex_hooks_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    with open(path) as f:
        hooks = json.load(f)["hooks"]
    assert "SubagentStart" in hooks and "PermissionRequest" in hooks
    assert "SessionEnd" not in hooks               # Codex has none
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "--harness codex" in cmd and cmd.endswith("--event PreToolUse")
