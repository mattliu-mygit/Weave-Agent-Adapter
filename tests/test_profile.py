"""Profile loading and dotted-path field extraction."""
from __future__ import annotations

from weave_agent_adapter.profile import Profile, load_profile


def test_every_profile_names_where_its_hooks_carry_the_working_directory():
    for harness in ("claude-code", "codex", "gemini-cli"):
        p = load_profile(harness)
        assert p.name == harness
        assert p.extract({"session_id": "s1", "cwd": "/repo"}) == {"cwd": "/repo"}


def test_field_extraction_resolves_dotted_paths_and_omits_missing_fields():
    p = Profile.from_dict({"harness": {"name": "example"},
                           "fields": {"cwd": "workspace.root", "branch": "git.branch"}})

    assert p.extract({"workspace": {"root": "/repo"}}) == {"cwd": "/repo"}


def test_registration_has_all_events():
    p = load_profile("claude-code")
    events = p.registration["events"]
    for required in ("SessionStart", "PreToolUse", "SubagentStop", "PreCompact", "SessionEnd"):
        assert required in events
