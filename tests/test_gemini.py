"""Gemini CLI uses the same reducer through a declarative profile."""
from __future__ import annotations

import json

from weave_agent_adapter.install import install
from weave_agent_adapter.profile import load_profile






def test_gemini_profile_omits_unsupported_optional_capabilities():
    profile = load_profile("gemini-cli")

    assert "PermissionRequest" not in profile.events
    assert "SubagentStart" not in profile.events
    assert profile.config_surface == {}
    assert profile.registration["command"].endswith("--success-json")


def test_gemini_install_targets_settings_json_and_requests_json_success(tmp_path):
    path = str(tmp_path / "settings.json")
    install("gemini-cli", path=path)

    hooks = json.loads((tmp_path / "settings.json").read_text())["hooks"]
    command = hooks["BeforeAgent"][0]["hooks"][0]["command"]
    assert "--harness gemini-cli --success-json" in command
    assert command.endswith("--event BeforeAgent")
