"""Codex's profile installs its hooks, which include no SessionEnd."""
from __future__ import annotations

import json

from weave_agent_adapter.install import install


def test_codex_install_targets_codex_hooks_json(tmp_path):
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    with open(path) as f:
        hooks = json.load(f)["hooks"]
    assert "SubagentStart" in hooks and "PermissionRequest" in hooks
    assert "SessionEnd" not in hooks               # Codex has none
    cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "--harness codex" in cmd and cmd.endswith("--event PreToolUse")
