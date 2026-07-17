"""Reducer behavior: event stream → finalized turns, sessions, sweep, threads."""
from __future__ import annotations

import json

from conftest import run, subagents_of, tools_of
from weave_agent_adapter.model import ToolStatus
from weave_agent_adapter.model import WireEvent
from weave_agent_adapter.profile import Profile, load_profile
from weave_agent_adapter.tracer import Tracer

SID = "s1"


def test_turn_finalized_on_session_end():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "do it"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"command": "ls"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"stdout": "ok"}}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, session), = turns
    assert session.project == "ent/proj"
    assert session.cwd == "/repo"
    assert turn.input_text == "do it"
    assert turn.output_text == "done"
    (tool,) = tools_of(turn, "Bash")
    assert tool.status == ToolStatus.OK
    assert not tr.sessions                        # popped on session_end


def test_session_retains_launch_trace_role():
    _, turns = run(
        [
            ("SessionStart", {"session_id": SID}),
            ("UserPromptSubmit", {"session_id": SID, "prompt": "judge"}),
            ("Stop", {"session_id": SID}),
            ("SessionEnd", {"session_id": SID}),
        ],
        trace_role="judge_evaluation",
    )

    (_, session), = turns
    assert session.trace_role == "judge_evaluation"


def test_conflicting_roles_on_one_session_fail_safe_to_other_system():
    tr = Tracer(load_profile("claude-code"), "ent/proj")
    tr.handle(
        WireEvent(
            "claude-code",
            "SessionStart",
            1.0,
            {"session_id": SID},
            trace_role="agent_session",
        )
    )
    tr.handle(
        WireEvent(
            "claude-code",
            "UserPromptSubmit",
            2.0,
            {"session_id": SID, "prompt": "judge"},
            trace_role="judge_evaluation",
        )
    )

    assert tr.sessions[SID].trace_role == "other_system"


def test_tool_correlation_by_use_id():
    # two tools open concurrently -> the close must match by tool_use_id
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_use_id": "a",
                         "tool_response": {"x": 1}}),
    ])
    t = tr.sessions[SID].current_turn
    assert t.tool_calls["a"].status == ToolStatus.OK
    assert t.tool_calls["b"].status == ToolStatus.RUNNING


def test_claude_failure_and_denial_use_documented_event_fields():
    _, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p",
                              "model": "claude-sonnet", "permission_mode": "default"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "failed"}),
        ("PostToolUseFailure", {"session_id": SID, "tool_name": "Bash",
                                "tool_use_id": "failed", "error": "exit 1"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Write", "tool_use_id": "denied"}),
        ("PermissionRequest", {"session_id": SID, "tool_use_id": "denied"}),
        ("PermissionDenied", {"session_id": SID, "tool_use_id": "denied",
                              "reason": "policy"}),
        ("Stop", {"session_id": SID}),
    ])

    (turn, _), = turns
    assert turn.model == "claude-sonnet"
    assert turn.permission_mode == "default"
    failed, denied = tools_of(turn)
    assert failed.status == ToolStatus.ERROR
    assert failed.error == "exit 1"
    assert denied.status == ToolStatus.REJECTED
    assert denied.permission.reason == "policy"


def test_midturn_prompt_is_steering_not_new_turn():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "first"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "actually, wait"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns                            # one turn, not two
    assert [item.text for item in turn.steering] == ["actually, wait"]


def test_stop_only_subagent_gets_identity_no_output():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "agent_type": "Explore", "agent_id": "a9",
                          "last_assistant_message": "not reliably the subagent's"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns
    (sub,) = subagents_of(turn, "Explore")
    assert sub["agent_id"] == "a9"
    assert sub["started_at"] == sub["ended_at"]
    assert "output" not in sub


def test_background_subagent_stop_is_ignored():
    # Claude Code fires SubagentStop for its own background agents (prompt
    # suggestions, title gen) with no agent_type; those must not create a node.
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "last_assistant_message": "Yes, do it"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns
    assert subagents_of(turn) == []


def test_background_stop_does_not_steal_real_subagent():
    # a background SubagentStop (different agent_id, no agent_type) must not close
    # the real tracked subagent; all its tools stay under one node.
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStart", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "a",
                        "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "a",
                         "agent_id": "g1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "bg99"}),   # background, no type
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b",
                        "agent_id": "g1", "agent_type": "general-purpose"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "b",
                         "agent_id": "g1", "tool_response": {}}),
        ("SubagentStop", {"session_id": SID, "agent_id": "g1", "agent_type": "general-purpose",
                          "last_assistant_message": "done"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns
    (sub,) = subagents_of(turn)                   # exactly one subagent record
    assert len([tool for tool in tools_of(turn, "Bash")
                if tool.agent_id == sub["agent_id"]]) == 2
    assert sub["ended_at"] == 1008.0
    assert "output" not in sub


def test_compaction_recorded_as_turn_event():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreCompact", {"session_id": SID, "trigger": "auto"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns
    assert turn.compactions == [(1002.0, "auto")]


def test_sampling_excludes_session():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SessionEnd", {"session_id": SID}),
    ], session_rate=0.0)
    assert turns == []
    assert not tr.sessions


def test_sweep_finalizes_stale_session_and_emits_pending_turn():
    tr, turns = run([("SessionStart", {"session_id": SID}),
                     ("UserPromptSubmit", {"session_id": SID, "prompt": "p"})], t0=1000.0)
    assert turns == []                            # turn still open, nothing emitted
    swept = tr.sweep(now=1000.0 + 10_000, ttl=60.0)
    assert swept == 1
    assert not tr.sessions
    (turn, _), = turns
    assert turn.incomplete is True


def test_turnless_session_emits_nothing():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/repo"}),
        ("SessionEnd", {"session_id": SID}),
    ])
    assert turns == []
    assert not tr.sessions


def test_session_autocreated_without_session_start():
    # resume/edit continue under a new session_id with no SessionStart; the turn
    # and its tools must still be traced (auto-created session).
    tr, turns = run([
        ("UserPromptSubmit", {"session_id": "resumed", "prompt": "keep going", "cwd": "/repo"}),
        ("PreToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": "resumed", "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"ok": 1}}),
        ("Stop", {"session_id": "resumed"}),
        ("SessionEnd", {"session_id": "resumed"}),
    ])
    (turn, _), = turns
    assert turn.input_text == "keep going"
    assert tools_of(turn, "Bash")


def test_project_per_repo_routes_by_cwd_leaf():
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "cwd": "/Users/me/my-repo"}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "hi"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], project="ent/default-proj", project_per_repo=True)
    (_, session), = turns
    assert session.project == "ent/my-repo"       # cwd leaf + configured entity


def test_thread_id_from_transcript_root(tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join([
        json.dumps({"type": "queue-operation"}),                       # no uuid: skipped
        json.dumps({"type": "user", "isSidechain": False, "parentUuid": None,
                    "uuid": "ROOT-UUID", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "parentUuid": "ROOT-UUID"}),
    ]))
    tr, turns = run([
        ("SessionStart", {"session_id": SID, "transcript_path": str(tp)}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "hi", "transcript_path": str(tp)}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (_, session), = turns
    assert session.thread_id == "ROOT-UUID"


def test_autocreated_session_creates_synthetic_turn_for_midturn_events():
    # sidecar restart: first event for this sid is a tool, not a turn_start;
    # a synthetic turn is created so the tool isn't dropped
    tr, turns = run([
        ("PreToolUse", {"session_id": "crash-resumed", "tool_name": "Bash", "tool_use_id": "t1"}),
        ("PostToolUse", {"session_id": "crash-resumed", "tool_name": "Bash", "tool_use_id": "t1",
                         "tool_response": {"ok": 1}}),
        ("Stop", {"session_id": "crash-resumed"}),
        ("SessionEnd", {"session_id": "crash-resumed"}),
    ])
    (turn, _), = turns
    assert turn.input_text == "(resumed)"
    assert tools_of(turn, "Bash")


def test_subagent_stop_with_no_agent_id_ignored():
    # agent_type present but agent_id missing → must not key subagents[None]
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("SubagentStop", {"session_id": SID, "agent_type": "Explore"}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ])
    (turn, _), = turns
    assert subagents_of(turn) == []


def test_redaction_applied_to_tool_input():
    from weave_agent_adapter.redact import Redactor
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_use_id": "t1",
                        "tool_input": {"api_key": "supersecret", "command": "ls"}}),
        ("Stop", {"session_id": SID}),
        ("SessionEnd", {"session_id": SID}),
    ], redactor=Redactor())
    (turn, _), = turns
    args = tools_of(turn, "Bash")[0].tool_input
    assert "supersecret" not in json.dumps(args)
    assert args["command"] == "ls"


def test_failed_emission_is_one_shot_and_does_not_block_next_turn():
    class Emitter:
        calls = 0

        def emit_turn(self, turn, session):
            self.calls += 1
            return False

    emitter = Emitter()
    tr = Tracer(load_profile("claude-code"), "ent/proj", emitter=emitter)
    for index, (event, payload) in enumerate([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "first"}),
        ("Stop", {"session_id": SID}),
    ]):
        tr.handle(WireEvent("claude-code", event, 1000.0 + index, payload))
    assert emitter.calls == 1
    tr.handle(WireEvent("claude-code", "UserPromptSubmit", 1201.0,
                        {"session_id": SID, "prompt": "second"}))
    assert emitter.calls == 1
    assert tr.sessions[SID].current_turn.input_text == "second"


def test_fallback_correlation_matches_tool_name_not_lifo():
    tr, _ = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Read"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash"}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Read", "tool_response": {}}),
    ])
    tools = tr.sessions[SID].current_turn.tool_calls
    assert [tool.status for tool in tools.values()] == [ToolStatus.OK, ToolStatus.RUNNING]


def test_ambiguous_fallback_does_not_steal_parallel_tool():
    tr, _ = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": "a"}}),
        ("PreToolUse", {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": "b"}}),
        ("PostToolUse", {"session_id": SID, "tool_name": "Bash", "tool_response": {}}),
    ])
    tools = list(tr.sessions[SID].current_turn.tool_calls.values())
    assert [tool.status for tool in tools[:2]] == [ToolStatus.RUNNING, ToolStatus.RUNNING]
    assert tools[2].status == ToolStatus.OK


def test_stop_immediately_emits_and_clears_active_work():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p", "turn_id": "turn-1"}),
        ("Stop", {"session_id": SID, "last_assistant_message": "done",
                  "turn_id": "turn-1"}),
    ], harness="codex")

    (turn, _), = turns
    assert turn.output_text == "done"
    assert turn.turn_id == "turn-1"
    assert tr.has_active_work() is False


def test_event_specific_fields_update_turn_and_mark_embedded_tool_error():
    profile = Profile.from_dict({
        "harness": {"name": "minimal"},
        "events": {
            "Begin": "turn_start",
            "Model": "turn_update",
            "ToolDone": "tool_post",
            "Done": "turn_end",
        },
        "fields": {
            "session_id": "session_id",
            "prompt": "prompt",
            "tool_name": "tool_name",
            "tool_input": "tool_input",
            "assistant_message": "response",
        },
        "event_fields": {
            "Model": {"model": "request.model"},
            "ToolDone": {"tool_error": "tool_response.error"},
        },
    })
    finalized = []

    class Emitter:
        def emit_turn(self, turn, session):
            finalized.append((turn, session))

    tr = Tracer(profile, "ent/proj", emitter=Emitter())
    for index, (event, payload) in enumerate([
        ("Begin", {"session_id": SID, "prompt": "fix it"}),
        ("Model", {"session_id": SID, "request": {"model": "model-1"}}),
        ("ToolDone", {"session_id": SID, "tool_name": "Shell",
                      "tool_input": {"command": "false"},
                      "tool_response": {"error": "exit 1"}}),
        ("Done", {"session_id": SID, "response": "handled"}),
    ]):
        tr.handle(WireEvent("minimal", event, 2000.0 + index, payload))

    (turn, _), = finalized
    assert turn.model == "model-1"
    (tool,) = tools_of(turn, "Shell")
    assert tool.status == ToolStatus.ERROR
    assert tool.error == "exit 1"
    assert tool.output is None


def test_pending_work_defers_turn_completion_until_clean_end():
    tr, turns = run([
        ("SessionStart", {"session_id": SID}),
        ("UserPromptSubmit", {"session_id": SID, "prompt": "p"}),
        ("Stop", {"session_id": SID, "last_assistant_message": "still working",
                  "background_tasks": [{"id": "task-1"}]}),
    ])

    assert turns == []
    assert tr.sessions[SID].current_turn.ended_at is None

    tr.handle(WireEvent("claude-code", "Stop", 1100.0, {
        "session_id": SID,
        "last_assistant_message": "done",
        "background_tasks": [],
    }))

    (turn, _), = turns
    assert turn.output_text == "done"
    assert turn.ended_at == 1100.0
