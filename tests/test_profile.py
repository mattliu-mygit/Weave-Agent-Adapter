"""Profile loading."""
from __future__ import annotations

from weave_agent_adapter.profile import load_profile


def test_every_profile_loads_under_its_harness_name():
    for harness in ("claude-code", "codex", "gemini-cli"):
        p = load_profile(harness)
        assert p.name == harness
        assert f"--harness {harness}" in p.registration["command"]


def test_registration_has_all_events():
    p = load_profile("claude-code")
    events = p.registration["events"]
    for required in ("SessionStart", "PreToolUse", "SubagentStop", "PreCompact", "SessionEnd"):
        assert required in events
