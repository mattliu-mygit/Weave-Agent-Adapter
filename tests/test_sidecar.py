"""Sidecar wire validation and active-work-aware lifecycle."""
from __future__ import annotations

import json
from types import SimpleNamespace

from weave_agent_adapter.sidecar import Sidecar


def _wire(**overrides):
    data = {
        "v": 1,
        "harness": "codex",
        "event": "SessionStart",
        "captured_at": 1.0,
        "payload": {"session_id": "s"},
    }
    data.update(overrides)
    return json.dumps(data).encode()


def test_sidecar_rejects_unsupported_wire_version(tmp_path, monkeypatch):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(v=2))
    assert handled == []


def test_sidecar_rejects_non_object_payload(tmp_path, monkeypatch):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(payload=["not", "an", "object"]))
    assert handled == []


def test_sidecar_rejects_non_finite_timestamp(tmp_path, monkeypatch):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(captured_at=float("inf")))
    assert handled == []


def test_sidecar_parses_trace_role_with_backward_compatible_default(tmp_path, monkeypatch):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(
        sidecar,
        "_tracer_for",
        lambda _harness: SimpleNamespace(handle=handled.append),
    )

    sidecar._handle_line(_wire(trace_role="signal_evaluation"))
    sidecar._handle_line(_wire())
    sidecar._handle_line(_wire(trace_role="future_role"))

    assert [wire.trace_role for wire in handled] == [
        "signal_evaluation",
        "agent_session",
        "other_system",
    ]


def test_idle_timeout_does_not_exit_with_active_work(tmp_path):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), idle_s=1.0)
    sidecar._last = 1.0
    sidecar.tracers["codex"] = SimpleNamespace(has_active_work=lambda: True)
    assert sidecar.can_idle_exit(now=10.0) is False


def test_idle_timeout_exits_after_work_is_emitted(tmp_path):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), idle_s=1.0)
    sidecar._last = 1.0
    sidecar.tracers["codex"] = SimpleNamespace(has_active_work=lambda: False)
    assert sidecar.can_idle_exit(now=10.0) is True


def test_only_a_harness_without_a_transcript_reader_gets_a_tracer(tmp_path):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"))

    assert sidecar._tracer_for("codex") is None
    assert sidecar._tracer_for("claude-code") is None
    assert sidecar._tracer_for("gemini-cli") is sidecar._tracer_for("gemini-cli")
