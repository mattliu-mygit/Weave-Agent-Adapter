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
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(v=2))
    assert handled == []


def test_sidecar_rejects_non_object_payload(tmp_path, monkeypatch):
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(payload=["not", "an", "object"]))
    assert handled == []


def test_sidecar_rejects_non_finite_timestamp(tmp_path, monkeypatch):
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"))
    handled = []
    monkeypatch.setattr(sidecar, "_tracer_for",
                        lambda harness: SimpleNamespace(handle=handled.append))
    sidecar._handle_line(_wire(captured_at=float("inf")))
    assert handled == []


def test_idle_timeout_does_not_exit_with_active_work(tmp_path):
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"), idle_s=1.0)
    sidecar._last = 1.0
    sidecar.tracers["codex"] = SimpleNamespace(has_active_work=lambda: True)
    assert sidecar.can_idle_exit(now=10.0) is False


def test_idle_timeout_exits_after_work_is_emitted(tmp_path):
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"), idle_s=1.0)
    sidecar._last = 1.0
    sidecar.tracers["codex"] = SimpleNamespace(has_active_work=lambda: False)
    assert sidecar.can_idle_exit(now=10.0) is True


def test_flush_emitter_reports_failure(tmp_path):
    emitter = SimpleNamespace(flush=lambda: False)
    sidecar = Sidecar("ent/proj", str(tmp_path / "sidecar.sock"), emitter=emitter)
    assert sidecar.flush_emitter() is False


def test_bare_project_is_deferred_to_weave(tmp_path):
    sidecar = Sidecar("project", str(tmp_path / "sidecar.sock"))

    first = sidecar._tracer_for("codex")
    second = sidecar._tracer_for("claude-code")

    assert first.project == "project"
    assert second.project == "project"
