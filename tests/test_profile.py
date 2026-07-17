"""Profile loading, event mapping, and dotted-path field extraction."""
from __future__ import annotations

from weave_agent_adapter.profile import Profile, load_profile


def test_claude_code_event_mapping():
    p = load_profile("claude-code")
    assert p.name == "claude-code"
    assert p.canonical_event("PreToolUse") == "tool_pre"
    assert p.canonical_event("SubagentStop") == "subagent_stop"
    assert p.canonical_event("PreCompact") == "compaction"
    assert p.canonical_event("NoSuchEvent") is None


def test_field_extraction_resolves_dotted_paths():
    p = load_profile("claude-code")
    payload = {"session_id": "s1", "tool_name": "Bash",
               "tool_input": {"command": "ls"}, "agent_type": "Explore"}
    fields = p.extract(payload)
    assert fields["session_id"] == "s1"
    assert fields["tool_name"] == "Bash"
    assert fields["tool_input"] == {"command": "ls"}
    assert fields["agent_type"] == "Explore"
    # unset fields are omitted, not None
    assert "denial_reason" not in fields


def test_registration_has_all_events():
    p = load_profile("claude-code")
    events = p.registration["events"]
    for required in ("SessionStart", "PreToolUse", "SubagentStop", "PreCompact", "SessionEnd"):
        assert required in events


def test_event_fields_override_common_paths_without_hiding_other_fields():
    p = Profile.from_dict({
        "harness": {"name": "example"},
        "events": {"ToolFailed": "tool_error"},
        "fields": {
            "session_id": "session_id",
            "tool_output": "tool_response",
        },
        "event_fields": {
            "ToolFailed": {"tool_error": "error.message"},
        },
    })

    fields = p.extract({
        "session_id": "s1",
        "tool_response": "partial output",
        "error": {"message": "command failed"},
    }, "ToolFailed")

    assert fields == {
        "session_id": "s1",
        "tool_output": "partial output",
        "tool_error": "command failed",
    }
