"""Current Weave OTLP endpoint and local exporter acceptance behavior."""
from __future__ import annotations

from types import SimpleNamespace

import weave_agent_adapter.emit as emit
from weave_agent_adapter.core.model import Session, Turn
from weave_agent_adapter.emit import DEFAULT_ENDPOINT, GenAITurnEmitter


def _turn_and_session():
    turn = Turn(index=0, started_at=1.0, open=False, input_text="hi",
                output_text="bye", ended_at=2.0)
    session = Session(session_id="s", project="weave-team/agent-sessions",
                      started_at=1.0, last_activity=2.0, harness="codex")
    return turn, session


def test_default_endpoint_matches_documented_weave_otel_endpoint():
    assert DEFAULT_ENDPOINT == "https://trace.wandb.ai/otel/v1/traces"


def test_missing_api_key_rejects_turn_and_diagnoses(monkeypatch):
    seen = []
    monkeypatch.setattr(emit, "_api_key", lambda: None)
    monkeypatch.setattr(emit, "diagnose", lambda phase, **fields: seen.append(phase),
                        raising=False)
    emitter = GenAITurnEmitter(default_entity="weave-team")
    turn, session = _turn_and_session()
    assert emitter.emit_turn(turn, session) is False
    assert "export_auth" in seen


def test_provider_initialization_error_rejects_turn(monkeypatch):
    emitter = GenAITurnEmitter(default_entity="weave-team")
    monkeypatch.setattr(emitter, "_tracer", lambda project_id: None)
    turn, session = _turn_and_session()
    assert emitter.emit_turn(turn, session) is False


def test_flush_combines_provider_results():
    emitter = GenAITurnEmitter(default_entity="weave-team", emit=lambda node, project: True)
    emitter._providers = {
        "weave-team/a": SimpleNamespace(force_flush=lambda: True),
        "weave-team/b": SimpleNamespace(force_flush=lambda: False),
    }
    assert emitter.flush() is False
