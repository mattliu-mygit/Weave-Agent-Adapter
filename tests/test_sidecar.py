"""Sidecar wire validation and active-work-aware lifecycle."""
from __future__ import annotations

import json

from weave_agent_adapter.sidecar import Sidecar


class _Sender:
    def __init__(self, handled, active=False):
        self.handled, self.active = handled, active

    def handle(self, wire):
        self.handled.append(wire)

    def has_active_work(self):
        return self.active


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
    handled = []
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), session_sender=_Sender(handled))
    sidecar._handle_line(_wire(v=2))
    assert handled == []


def test_sidecar_rejects_non_object_payload(tmp_path, monkeypatch):
    handled = []
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), session_sender=_Sender(handled))
    sidecar._handle_line(_wire(payload=["not", "an", "object"]))
    assert handled == []


def test_sidecar_rejects_non_finite_timestamp(tmp_path, monkeypatch):
    handled = []
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), session_sender=_Sender(handled))
    sidecar._handle_line(_wire(captured_at=float("inf")))
    assert handled == []


def test_sidecar_parses_trace_role_with_backward_compatible_default(tmp_path, monkeypatch):
    handled = []
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), session_sender=_Sender(handled))

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
    sidecar.session_sender = _Sender([], active=True)
    assert sidecar.can_idle_exit(now=10.0) is False


def test_idle_timeout_exits_after_work_is_emitted(tmp_path):
    sidecar = Sidecar(str(tmp_path / "sidecar.sock"), idle_s=1.0)
    sidecar._last = 1.0
    sidecar.session_sender = _Sender([], active=False)
    assert sidecar.can_idle_exit(now=10.0) is True


def test_sidecar_handles_spooled_events_oldest_first_and_empties_the_spool(tmp_path, monkeypatch):
    from weave_agent_adapter import transport

    socket_path = str(tmp_path / "sidecar.sock")
    for event in ("SessionStart", "Stop"):
        assert transport.spool({"v": 1, "event": event}, socket_path)
    sidecar = Sidecar(socket_path)
    handled = []
    monkeypatch.setattr(sidecar, "_handle_line", lambda line: handled.append(json.loads(line)["event"]))

    sidecar._drain_spool()

    assert handled == ["SessionStart", "Stop"]
    assert transport.take_spool(socket_path) == []


def test_the_spool_refuses_events_past_its_bound(tmp_path, monkeypatch):
    from weave_agent_adapter import transport

    socket_path = str(tmp_path / "sidecar.sock")
    monkeypatch.setattr(transport, "SPOOL_MAX_BYTES", 40)

    assert transport.spool({"event": "a"}, socket_path)
    assert not transport.spool({"event": "b" * 40}, socket_path)
    assert len(transport.take_spool(socket_path)) == 1
