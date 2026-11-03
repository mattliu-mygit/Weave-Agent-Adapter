"""Workspace-local trace-role resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from weave_agent_adapter.trace_role import resolve_trace_role


def _write_role(directory: Path, value: str) -> Path:
    path = directory / ".weave-agent-adapter" / "trace-role"
    path.parent.mkdir(parents=True)
    path.write_text(value)
    return path


def test_non_empty_environment_role_overrides_workspace_file(tmp_path):
    _write_role(tmp_path, "judge_evaluation")

    assert resolve_trace_role(
        tmp_path,
        {"WEAVE_AGENT_TRACE_ROLE": "signal_evaluation"},
    ) == "signal_evaluation"


def test_empty_environment_role_falls_through_to_workspace_file(tmp_path):
    _write_role(tmp_path, "judge_evaluation")

    assert resolve_trace_role(
        tmp_path,
        {"WEAVE_AGENT_TRACE_ROLE": "  "},
    ) == "judge_evaluation"


def test_nearest_workspace_file_wins(tmp_path):
    nested = tmp_path / "repo" / "package"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    _write_role(tmp_path, "signal_evaluation")
    _write_role(tmp_path / "repo", "reflection_evaluation")

    assert resolve_trace_role(nested, {}) == "reflection_evaluation"


def test_parent_workspace_file_is_found_from_nested_directory(tmp_path):
    nested = tmp_path / "repo" / "src" / "package"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    _write_role(tmp_path / "repo", "judge_evaluation")

    assert resolve_trace_role(nested, {}) == "judge_evaluation"


def test_search_stops_at_repository_boundary(tmp_path):
    nested = tmp_path / "repo" / "src"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    _write_role(tmp_path, "judge_evaluation")

    assert resolve_trace_role(nested, {}) == "agent_session"


@pytest.mark.parametrize("value", [None, "", "  \n"])
def test_missing_or_empty_workspace_file_uses_default(tmp_path, value):
    if value is not None:
        _write_role(tmp_path, value)

    assert resolve_trace_role(tmp_path, {}) == "agent_session"


def test_unreadable_workspace_file_uses_default(tmp_path, monkeypatch):
    path = _write_role(tmp_path, "judge_evaluation")
    original_read_text = Path.read_text

    def unreadable(selector, *args, **kwargs):
        if selector == path:
            raise PermissionError("not readable")
        return original_read_text(selector, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    assert resolve_trace_role(tmp_path, {}) == "agent_session"


def test_undecodable_workspace_file_uses_default(tmp_path):
    path = _write_role(tmp_path, "judge_evaluation")
    path.write_bytes(b"\xff")

    assert resolve_trace_role(tmp_path, {}) == "agent_session"


@pytest.mark.parametrize("source", ["environment", "file"])
def test_unknown_explicit_role_fails_safe_to_other_system(tmp_path, source):
    environ = {}
    if source == "environment":
        environ["WEAVE_AGENT_TRACE_ROLE"] = "future_role"
    else:
        _write_role(tmp_path, "future_role")

    assert resolve_trace_role(tmp_path, environ) == "other_system"


def test_workspace_file_allows_surrounding_whitespace(tmp_path):
    _write_role(tmp_path, "  reflection_evaluation\n")

    assert resolve_trace_role(tmp_path, {}) == "reflection_evaluation"
