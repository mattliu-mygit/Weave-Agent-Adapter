"""Reducer behavior: the event stream → span tree, permissions, sweep."""
from __future__ import annotations

from conftest import NS, run, starts, ends, one, end_of
from weave_agent_adapter.redact import Redactor

SID = "s1"


def test_session_turn_tool_nesting():
    tr, sink = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "do it"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "ls"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"stdout": "ok"}}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    session = one(sink, f"{NS}.session")
    turn = one(sink, f"{NS}.turn")
    tool = one(sink, f"{NS}.tool.Bash")
    assert session.parent_id is None
    assert turn.parent_id == session.id
    assert tool.parent_id == turn.id
    # session closed with a turn count
    assert end_of(sink, session.id).output["turn_count"] == 1
    assert not tr.sessions            # popped on session_end


def test_tool_correlation_by_use_id():
    # two tools opened before either closes → close must match by tool_use_id
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a",
                         "tool_response": {"x": 1}}),
    ])
    read = one(sink, f"{NS}.tool.Read")
    assert end_of(sink, read.id) is not None          # 'a' closed
    bash = one(sink, f"{NS}.tool.Bash")
    assert end_of(sink, bash.id) is None              # 'b' still open


def test_permission_denied_closes_rejected():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PermissionDenied", {"session_id": SID, "tool_use_id": "t1",
                              "denial_reason": "nope"}),
    ])
    tool = one(sink, f"{NS}.tool.Bash")
    e = end_of(sink, tool.id)
    attrs = e.attributes[NS]
    assert attrs["status"] == "rejected"
    assert attrs["permission_decision"] == "deny"
    assert attrs["denial_reason"] == "nope"


def test_permission_allow_inferred_from_post():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"stdout": "ok"}}),
    ])
    e = end_of(sink, one(sink, f"{NS}.tool.Bash").id)
    assert e.attributes[NS]["permission_decision"] == "allow"


def test_turn_captures_prompt_and_assistant_reply():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "add logging"}),
        ("Stop", {"session_id": SID, "last_assistant_message": "Done, added logging."}),
    ])
    turn = one(sink, f"{NS}.turn")
    assert turn.inputs["prompt"] == "add logging"          # prompt on the turn's input
    assert end_of(sink, turn.id).output["assistant"] == "Done, added logging."
    # the input marker still carries the prompt too
    assert one(sink, f"{NS}.input").inputs["prompt"] == "add logging"


def test_project_per_repo_stamps_project_from_cwd():
    from weave_agent_adapter.profile import load_profile
    from weave_agent_adapter.sinks.recording import RecordingSink
    from weave_agent_adapter.tracer import Tracer
    from weave_agent_adapter.core.model import WireEvent
    tr = Tracer(load_profile("claude-code"), "default-proj", RecordingSink(),
                project_per_repo=True)
    tr.handle(WireEvent(1, "claude-code", "SessionStart", 1.0,
                        {"session_id": SID, "cwd": "/Users/me/my-repo"}, 1))
    tr.handle(WireEvent(1, "claude-code", "UserPromptSubmit", 1.5,
                        {"session_id": SID, "prompt": "hi"}, 1))   # surfaces the session
    session = one(tr.sink, f"{NS}.session")
    assert session.project == "my-repo"           # leaf of cwd, not the default
    # a session with no cwd falls back to the configured default
    tr.handle(WireEvent(1, "claude-code", "SessionStart", 2.0, {"session_id": "s2"}, 1))
    tr.handle(WireEvent(1, "claude-code", "UserPromptSubmit", 2.5, {"session_id": "s2", "prompt": "hi"}, 1))
    s2 = [c for c in starts(tr.sink) if c.op_name == f"{NS}.session"][1]
    assert s2.project == "default-proj"


def test_midturn_prompt_is_steering():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "first"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "actually, wait"}),
    ])
    assert len([c for c in starts(sink) if c.op_name == f"{NS}.turn"]) == 1
    assert one(sink, f"{NS}.steering")


def test_subagent_stop_annotation():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "agent_type": "Explore", "agent_id": "a9",
                          "last_assistant_message": "found 3 files"}),
    ])
    agent = one(sink, f"{NS}.agent.Explore")
    assert agent.inputs["agent_type"] == "Explore"
    assert agent.inputs["agent_id"] == "a9"
    # stop-only: no output (last_assistant_message isn't reliably the subagent's reply)
    assert end_of(sink, agent.id).output is None


def test_background_subagent_stop_is_ignored():
    # Claude Code fires SubagentStop for its own background agents (prompt
    # suggestions, title gen) with no agent_type; those must not create a span.
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "last_assistant_message": "Yes, do it"}),
    ])
    assert not [c for c in starts(sink) if ".agent" in c.op_name]


def test_subagent_interior_tool_nests_under_subagent():
    # Claude Code has no SubagentStart: an interior tool (agent_id set) must
    # lazily open the subagent span and nest under it, then SubagentStop closes it.
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "launch"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Agent", "tool_use_id": "launch",
                         "tool_response": {}}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "inner",
                        "agent_id": "a9", "agent_type": "Explore"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "inner",
                         "agent_id": "a9", "tool_response": {"stdout": "x"}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "a9", "agent_type": "Explore",
                          "last_assistant_message": "done"}),
    ])
    turn = one(sink, f"{NS}.turn")
    agent = one(sink, f"{NS}.agent.Explore")
    launcher = [c for c in starts(sink) if c.op_name == f"{NS}.tool.Agent"][0]
    inner = [c for c in starts(sink) if c.op_name == f"{NS}.tool.Bash"][0]
    assert launcher.parent_id == turn.id          # launcher is under the turn
    assert agent.parent_id == turn.id             # subagent span under the turn
    assert inner.parent_id == agent.id            # interior tool under the subagent
    assert end_of(sink, agent.id).output == "done"


def test_compaction_under_session_root():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("PreCompact", {"session_id": SID, "trigger": "auto"}),
    ])
    session = one(sink, f"{NS}.session")
    comp = one(sink, f"{NS}.compaction")
    assert comp.parent_id == session.id
    assert comp.attributes[NS]["trigger"] == "auto"


def test_session_autocreated_without_session_start():
    # resume/edit continue under a new session_id with no SessionStart; the turn
    # and its tools must still be traced (auto-created session).
    tr, sink = run([
        ("UserPromptSubmit", {"session_id": "resumed", "prompt": "keep going", "cwd": "/repo"}),
        ("PreToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"ok": 1}}),
    ])
    assert one(sink, f"{NS}.session")                      # session materialized
    assert one(sink, f"{NS}.turn").inputs["prompt"] == "keep going"
    assert one(sink, f"{NS}.tool.Bash")


def test_subagent_interior_after_turn_stop_still_nests():
    # subagents run async: SubagentStart + interior tools + SubagentStop can all
    # arrive AFTER the turn's Stop. They must still nest under the subagent span.
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStart", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose"}),
        ("Stop", {"session_id": SID}),                     # turn ends while subagent runs
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "i1",
                        "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "i1",
                         "agent_id": "g1", "tool_response": {"ok": 1}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose"}),
    ])
    agent = one(sink, f"{NS}.agent.general-purpose")
    inner = one(sink, f"{NS}.tool.Bash")
    assert inner.parent_id == agent.id                    # interior tool nests under subagent


def test_turnless_session_emits_nothing():
    # a session opened and closed with no user prompt (background/quick-open) is dropped
    tr, sink = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    assert starts(sink) == []
    assert not tr.sessions


def test_sampling_excludes_session():
    tr, sink = run([("SessionStart", {"session_id": SID})], session_rate=0.0)
    assert starts(sink) == []
    assert not tr.sessions


def test_sweep_finalizes_stale_session():
    tr, sink = run([("SessionStart", {"session_id": SID}),
                    ("UserPromptSubmit", {"session_id": SID, "prompt": "p"})], t0=1000.0)
    # a turn was opened, so the session is live; sweep well past the ttl
    swept = tr.sweep(now=1000.0 + 10_000, ttl=60.0)
    assert swept == 1
    assert not tr.sessions
    e = end_of(sink, one(sink, f"{NS}.session").id)
    assert e.output["incomplete"] is True


def test_redaction_applied_to_tool_input():
    tr, sink = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"api_key": "supersecret", "command": "ls"}}),
    ], redactor=Redactor())
    tool = one(sink, f"{NS}.tool.Bash")
    assert tool.inputs["tool_input"]["api_key"] == "[REDACTED]"
    assert tool.inputs["tool_input"]["command"] == "ls"
